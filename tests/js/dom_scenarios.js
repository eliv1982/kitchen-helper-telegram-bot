"use strict";
/*
 * app.js, the page's own script, run against the shell the server sends (a mini DOM, see
 * fake_dom.js), a scripted server and fake timers. These check what the drawing code does with
 * what it is given: hostile text stays text, the composer and the buttons do what they say, the
 * drawer, the sign-out. They do not check how anything looks (no layout, no style) and a real
 * browser can differ from the mini DOM; see the README for what was tried in a real one.
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const H = require("./harness.js");
const { parseShell } = require("./fake_dom.js");

const { Logic, CONV_A, CONV_B, CONV_C, flush, json, text, fail, conversation, listBody, message, historyBody, turnBody, apiError, me, notAuthenticated } = H;

const STATIC = path.join(__dirname, "..", "..", "house_helper", "web", "static");
const APP_SOURCE = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const LOGIC_SOURCE = fs.readFileSync(path.join(STATIC, "chat-logic.js"), "utf8");

const scenarios = {};
function scenario(name, fn) {
  assert.ok(!scenarios[name], "duplicate " + name);
  scenarios[name] = fn;
}

let shellHtml = null;
function useShell(html) {
  shellHtml = html;
}

// The shell of a deployment with no way of signing in at all (what a server with login not connected sends).
let noLoginShellHtml = null;
function useNoLoginShell(html) {
  noLoginShellHtml = html;
}

const URL_A = `/api/conversations/${CONV_A}/messages`;
const HOSTILE = [
  '<img src=x onerror=alert(1)>',
  '<script>alert(2)</script>',
  '"><svg onload=alert(3)>',
  "javascript:alert(4)",
  "&lt;b&gt;already escaped&lt;/b&gt;",
  "<a href='https://evil.example'>click</a>",
  "</div></article><button onclick=alert(5)>",
].join("\n");

// --- the page -----------------------------------------------------------------------------------

function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

async function open(
  configure = (env) => env.signedIn([]),
  { narrow = false, shell = shellHtml, structured = false, confirm = undefined } = {}
) {
  const env = H.makeEnv({ structuredRecipes: structured });
  configure(env);
  const doc = parseShell(shell);
  if (structured) {
    // The shell the server sends for a deployment with the switch on; the flag is the
    // only difference, and it is read by app.js as the page starts.
    doc.body.attrs.set("data-structured-recipes", "1");
  }
  const change = [];
  const windowListeners = {};
  // The page reads the time as Date.now(); here it is the scenario's clock, not the day the test runs on, so
  // that what is drawn ("11:00", "вчера, 11:00", "18 октября") and when a session ends do not depend on today.
  class PageDate extends Date {
    constructor(...args) {
      super(...(args.length ? args : [env.timers.now]));
    }

    static now() {
      return env.timers.now;
    }
  }
  const sandbox = {
    Date: PageDate,
    document: doc,
    AbortController,
    console,
    sessionStorage: env.storage,
    crypto: env.crypto,
    fetch: env.net.fetch,
    setTimeout: env.timers.setTimeout,
    clearTimeout: env.timers.clearTimeout,
    setInterval: env.timers.setInterval,
    clearInterval: env.timers.clearInterval,
    matchMedia: () => ({ matches: narrow, addEventListener: (type, fn) => change.push(fn) }),
    addEventListener: (type, fn) => (windowListeners[type] = [...(windowListeners[type] || []), fn]),
  };
  if (confirm !== undefined) {
    sandbox.confirm = confirm;
  }
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(LOGIC_SOURCE, context, { filename: "chat-logic.js" });
  vm.runInContext(APP_SOURCE, context, { filename: "app.js" });
  const page = {
    env,
    doc,
    window: sandbox,
    el: (id) => {
      const found = doc.getElementById(id);
      assert.ok(found, "no element #" + id);
      return found;
    },
    async settle() {
      await flush();
      await flush();
    },
    // All the text in the page, hidden parts included: what a script, an extension or a find-in-page could read.
    root: () => doc.root.textContent,
    messages: () => page.el("messages"),
    articles: () => page.el("messages").byTag("article"),
    articleTexts: () => page.articles().map((a) => a.textContent),
    type(value, id = "composer-input") {
      const input = page.el(id);
      input.value = value;
      input.fire("input");
    },
    // Type into a field the way a browser reports it: the new value, then the input event.
    editField(node, value) {
      node.value = value;
      node.fire("input");
    },
    async send(value) {
      page.type(value);
      page.el("send").click();
      await page.settle();
    },
    visible: (id) => !page.el(id).hidden,
    narrowChanged: () => change.forEach((fn) => fn({})),
    fireWindow: (type, event = {}) => (windowListeners[type] || []).forEach((fn) => fn({ type, ...event })),
  };
  await page.settle();
  return page;
}

const signedInWith = (conversations, histories) => (env) => env.signedIn(conversations, histories);
const oneConversation = (extra = []) =>
  signedInWith([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"), ...extra]),
  });

// ============================================================================================
// The screens
// ============================================================================================

scenario("signed_out_shows_the_login_screen_with_the_configured_choices", async () => {
  const page = await open((env) => env.net.route("GET", "/auth/me", notAuthenticated()));

  assert.equal(page.visible("screen-login"), true);
  assert.deepEqual(["gate", "screen-loading", "screen-unavailable", "screen-app", "chat"].map(page.visible), [true, false, false, false, false]);
  assert.equal(page.el("login-telegram").getAttribute("href"), "/auth/telegram/login");
  assert.ok(page.el("email-form"));
  assert.equal(page.doc.activeElement, page.el("login-title"));
  assert.equal(page.el("login-notice").hidden, true);
  assert.equal(page.el("gate").getAttribute("aria-busy"), "false");
});

scenario("an_unreachable_service_is_not_the_login_screen_and_retry_goes_on", async () => {
  const page = await open((env) => {
    env.net.route("GET", "/auth/me", fail(), me());
    env.net.route("GET", "/api/conversations", listBody([]));
  });

  assert.equal(page.visible("screen-unavailable"), true);
  assert.equal(page.visible("screen-login"), false);
  assert.equal(page.el("unavailable-text").textContent, Logic.TEXT.network);
  assert.equal(page.el("unavailable-id").hidden, true, "no correlation id is invented for /auth");
  page.el("unavailable-retry").click();
  await page.settle();
  assert.equal(page.visible("screen-app"), true);
  assert.equal(page.visible("screen-unavailable"), false);
});

scenario("login_not_connected_shows_the_calm_setup_screen_and_not_the_failure_screen", async () => {
  const page = await open((env) => env.net.route("GET", "/auth/me", json(503, { detail: "login_unavailable" })), { shell: noLoginShellHtml });

  assert.deepEqual(["gate", "screen-setup"].map(page.visible), [true, true]);
  assert.deepEqual(["screen-loading", "screen-unavailable", "screen-login", "screen-app", "chat"].map(page.visible), [false, false, false, false, false]);
  assert.equal(page.el("setup-title").textContent, "House Helper");
  assert.equal(page.el("setup-text").textContent, "Вход пока не подключен. Мы готовим тестовую версию House Helper.");
  assert.equal(page.doc.activeElement, page.el("setup-title"));
  assert.equal(page.el("gate").getAttribute("aria-busy"), "false");

  // Not dressed as a failure: no alert, no error colour, nothing said in the alert region, no id, nothing to press.
  assert.equal(page.el("setup-text").getAttribute("role"), null);
  assert.ok(!page.el("setup-text").className.includes("notice-error"));
  assert.equal(page.el("alerter").textContent, "");
  assert.equal(page.el("unavailable-text").textContent, "");
  assert.equal(page.el("unavailable-id").textContent, "");
  assert.equal(page.el("screen-setup").byTag("button").length, 0);

  // Nothing of the conversations is drawn, nothing is stored, and the page asked once and does not ask again.
  assert.equal(page.el("conversation-list").elements().length, 0);
  assert.equal(page.env.storage.record, null);
  assert.equal(page.env.net.log.length, 1);
  await page.env.timers.advance(120000);
  assert.equal(page.env.net.log.length, 1);
});

scenario("real_failures_still_get_the_failure_screen_when_login_is_not_connected_here", async () => {
  const answers = [
    json(503, { detail: "temporarily_unavailable" }),
    json(503, { detail: "service_unavailable" }),
    json(500, { detail: "server_error" }),
    text(502, "<html>Bad Gateway</html>"),
    text(503, "login_unavailable"),
    json(502, { detail: "login_unavailable" }),
    fail(),
  ];
  for (const answer of answers) {
    const page = await open((env) => env.net.route("GET", "/auth/me", answer), { shell: noLoginShellHtml });
    const label = JSON.stringify(answer);

    assert.equal(page.visible("screen-unavailable"), true, label);
    assert.equal(page.visible("screen-setup"), false, label);
    assert.equal(page.visible("screen-login"), false, label);
    assert.equal(page.el("unavailable-text").getAttribute("role"), "alert", label);
    assert.ok(page.el("unavailable-text").textContent.length > 0, label);
    assert.ok(!page.root().includes("login_unavailable"), "a code is never shown to a person: " + label);
    assert.equal(page.el("unavailable-retry").textContent, Logic.TEXT.retry, label);
  }
});

scenario("the_signed_in_page_is_called_house_helper_and_the_gate_screens_stay_out_of_the_way", async () => {
  const page = await open(oneConversation());

  assert.equal(page.visible("screen-app"), true);
  assert.deepEqual(["gate", "screen-setup", "screen-unavailable", "screen-login"].map(page.visible), [false, false, false, false]);
  assert.deepEqual(page.el("screen-app").byClass("brand").map((node) => node.textContent), ["House Helper"]);
  assert.ok(page.root().includes("House Helper") && !page.root().includes("Кухонный помощник") && !page.root().includes("Kitchen Helper"));
  assert.ok(page.articleTexts().some((text) => text.includes("Старый вопрос")), "the conversation is drawn as before");
});

scenario("the_application_lists_conversations_and_shows_the_latest_with_who_said_what", async () => {
  const page = await open(signedInWith([conversation(CONV_B, 30), conversation(CONV_A, 0)], {
    [CONV_B]: historyBody(CONV_B, [message(5, "user", "Про суп"), message(6, "assistant", "Суп так.\n\nИ так.")]),
  }));

  assert.equal(page.visible("screen-app"), true);
  assert.equal(page.visible("chat"), true);
  assert.equal(page.visible("gate"), false);
  const items = page.el("conversation-list").byClass("conv-item");
  assert.equal(items.length, 2);
  assert.deepEqual(items.map((b) => b.getAttribute("aria-current")), ["true", null]);
  assert.match(page.el("chat-title").textContent, /^Разговор от 18 октября, \d\d:\d\d$/);
  assert.deepEqual(page.articleTexts(), ["Вы:Про суп" + "", "Помощник:Суп так.И так."].map((t, i) => page.articles()[i].textContent));
  const [mine, theirs] = page.articles();
  assert.ok(mine.className.includes("msg-user") && theirs.className.includes("msg-assistant"));
  assert.equal(mine.byClass("visually-hidden")[0].textContent, "Вы:");
  assert.equal(theirs.byClass("visually-hidden")[0].textContent, "Помощник:");
  assert.equal(theirs.byTag("time").length, 1);
  assert.equal(theirs.byTag("time")[0].getAttribute("datetime"), "2026-10-18T11:00:06.000Z");
});

// ============================================================================================
// Text is text
// ============================================================================================

scenario("hostile_text_is_only_ever_shown_as_text", async () => {
  const hostileTitle = "<img src=x onerror=alert(5)>";
  const page = await open(signedInWith([conversation(CONV_A, 0, hostileTitle)], {
    [CONV_A]: historyBody(CONV_A, [
      message(1, "user", HOSTILE),
      message(2, "assistant", HOSTILE),
      message(3, "user", HOSTILE, { status: "failed", idempotency_key: "k-3", error_code: "provider_error" }),
    ]),
  }));

  const allowed = new Set(["ARTICLE", "P", "DIV", "TIME", "SPAN", "BUTTON"]);
  for (const element of page.messages().elements()) {
    assert.ok(allowed.has(element.tagName), "an element from text: " + element.tagName);
  }
  for (const element of page.el("conversation-list").elements()) {
    assert.ok(["LI", "BUTTON", "SPAN"].includes(element.tagName), element.tagName);
  }
  const shown = page.messages().textContent;
  assert.equal(shown.split(HOSTILE.replace(/\n+/g, "")).length >= 1, true);
  for (const line of HOSTILE.split("\n")) {
    assert.ok(shown.includes(line), "shown verbatim: " + line);
  }
  assert.ok(page.el("conversation-list").textContent.includes(hostileTitle));
  assert.ok(page.el("chat-title").textContent === hostileTitle);

  // Nothing the script set on any element names a handler or a style or points anywhere.
  for (const [tag, name, value] of page.doc.attributeLog) {
    assert.ok(!/^on/i.test(name) && name !== "style" && name !== "href" && name !== "src" && name !== "srcdoc", `${tag}[${name}]`);
    assert.ok(!/alert\(|<|javascript:/i.test(value), `${tag}[${name}]=${value}`);
  }
  assert.equal(page.doc.root.byTag("img").length + page.doc.root.byTag("svg").length, 0);
  assert.equal(page.doc.root.byTag("script").length, 2, "only the two the server wrote");
});

scenario("paragraphs_are_kept_and_nothing_else_is_made_of_the_text", async () => {
  const recipe = "Борщ\n\nИнгредиенты:\n- свёкла\n- капуста\n\n\n\n1. Сварить.\r\n2. Подать.";
  const page = await open(signedInWith([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "assistant", recipe)]),
  }));

  const paragraphs = page.articles()[0].byClass("msg-text")[0].byTag("p").map((p) => p.textContent);
  assert.deepEqual(paragraphs, ["Борщ", "Инградиенты:\n- свёкла\n- капуста".replace("Инградиенты", "Ингредиенты"), "1. Сварить.\n2. Подать."]);
});

scenario("a_long_answer_takes_the_reading_width_and_a_short_one_keeps_its_bubble", async () => {
  const recipe = [
    "Борщ классический",
    "4 порции",
    "",
    "Ингредиенты:",
    "- Свекла — 2 шт.",
    "- Курица — 500 г",
    "- Капуста — 300 г",
    "- Картофель — 3 шт.",
    "",
    "Приготовление:",
    "1. Сварите бульон из курицы.",
    "2. Добавьте свеклу и капусту.",
    "3. Варите до готовности.",
  ].join("\n");
  const page = await open(signedInWith([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [
      message(1, "user", "Что приготовить?"),
      message(2, "assistant", recipe),
      message(3, "assistant", "Готово!"),
      message(4, "user", recipe),
    ]),
  }));

  const [question, answer, short, typed] = page.articles();
  assert.ok(answer.className.includes("msg-wide"), "a long recipe takes the reading column");
  assert.ok(!short.className.includes("msg-wide"), "a short reply keeps its bubble");
  assert.ok(!question.className.includes("msg-wide"), "a short question is unchanged");
  assert.ok(!typed.className.includes("msg-wide"), "the person's long message is unchanged");
});

// ============================================================================================
// Older messages
// ============================================================================================

scenario("older_messages_are_added_above_and_the_reader_keeps_their_place", async () => {
  const latest = Array.from({ length: 50 }, (_, k) => message(51 + k, k % 2 ? "assistant" : "user", "m" + (51 + k)));
  const page = await open(signedInWith([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, latest, { hasMore: true, nextBefore: 51 }) }));
  const box = page.messages();
  Object.defineProperty(box, "scrollHeight", { get: () => 120 + box.byTag("article").length * 100 });
  box.clientHeight = 400;

  assert.equal(page.el("messages").buttons("Показать более ранние сообщения").length, 1);
  box.scrollTop = 0; // the reader is at the top, looking at the oldest message they have
  page.env.net.route("GET", new RegExp(URL_A + "\\?limit=50&before=51$"),
    historyBody(CONV_A, Array.from({ length: 50 }, (_, k) => message(1 + k, k % 2 ? "assistant" : "user", "m" + (1 + k)))));
  page.el("messages").buttons("Показать более ранние сообщения")[0].click();
  await page.settle();

  assert.equal(page.articles().length, 100);
  assert.ok(page.articleTexts()[0].includes("m1") && page.articleTexts()[99].includes("m100"));
  assert.equal(box.scrollTop, 50 * 100, "scrolled down by exactly what was added above");
  assert.equal(page.el("messages").buttons("Показать более ранние сообщения").length, 0);
});

// ============================================================================================
// The composer
// ============================================================================================

scenario("a_message_typed_and_sent_is_one_request_a_pending_bubble_and_then_the_reply", async () => {
  const gate = deferred();
  const page = await open(signedInWith([conversation(CONV_A, 0)]));
  page.env.net.route("POST", URL_A, async () => {
    await gate.promise;
    return turnBody(CONV_A, 1, "Что из яиц?", 2, "Омлет.");
  });

  page.type("  Что из яиц?  ");
  assert.equal(page.el("counter").textContent, "11 / 2000");
  page.el("send").click();
  page.el("send").click(); // a second click while the first is in flight
  await page.settle();

  assert.equal(page.env.posts(CONV_A).length, 1);
  assert.equal(page.el("composer-input").value, "", "the text left the box when it was accepted");
  assert.equal(page.el("composer-input").readOnly, true);
  assert.equal(page.el("send").getAttribute("aria-disabled"), "true");
  assert.ok(page.el("composer-hint").textContent.includes("Дождитесь ответа"));
  const pending = page.messages().byClass("msg-pending");
  assert.equal(pending.length, 1);
  assert.ok(pending[0].textContent.includes("Что из яиц?") && /Отправляем|готовит/.test(pending[0].textContent));
  assert.equal(pending[0].byClass("spinner").length, 1);
  // The time here is the one thing that is about the clock: the page reads the scenario's, not the day of the run.
  assert.deepEqual([pending[0].byTag("time")[0].textContent, pending[0].byTag("time")[0].getAttribute("datetime")], ["12:00", "2026-10-18T12:00:00.000Z"]);

  gate.resolve();
  await page.settle();
  assert.equal(page.messages().byClass("msg-pending").length, 0);
  // Who said what, not when: the time of a message is drawn by its own scenario (and the page's clock is the scenario's).
  const said = page.articles().map((a) => [a.className.includes("msg-user") ? "user" : "assistant", a.byClass("msg-text")[0].textContent]);
  assert.deepEqual(said, [["user", "Что из яиц?"], ["assistant", "Омлет."]]);
  assert.equal(page.el("composer-input").readOnly, false);
  assert.equal(page.el("send").getAttribute("aria-disabled"), "false");
  await page.env.timers.advance(100);
  assert.equal(page.el("announcer").textContent, Logic.TEXT.replied);
});

scenario("enter_makes_a_new_line_and_only_control_or_command_enter_sends", async () => {
  const page = await open(signedInWith([conversation(CONV_A, 0)]));
  page.env.net.route("POST", URL_A, turnBody(CONV_A, 1, "строка", 2, "ответ"));
  page.type("строка");
  const input = page.el("composer-input");

  const plain = input.fire("keydown", { key: "Enter" });
  const composing = input.fire("keydown", { key: "Enter", ctrlKey: true, isComposing: true });
  const other = input.fire("keydown", { key: "a", ctrlKey: true });
  await page.settle();
  assert.deepEqual([plain.defaultPrevented, composing.defaultPrevented, other.defaultPrevented], [false, false, false]);
  assert.equal(page.env.posts(CONV_A).length, 0);

  const sent = input.fire("keydown", { key: "Enter", ctrlKey: true });
  await page.settle();
  assert.equal(sent.defaultPrevented, true);
  assert.equal(page.env.posts(CONV_A).length, 1);
  page.type("ещё");
  const meta = input.fire("keydown", { key: "Enter", metaKey: true });
  assert.equal(meta.defaultPrevented, true);
});

scenario("an_empty_or_too_long_message_is_refused_in_the_box_and_the_complaint_goes_away_when_typing", async () => {
  const page = await open(signedInWith([conversation(CONV_A, 0)]));

  await page.send("   ");
  assert.equal(page.el("composer-error").hidden, false);
  assert.equal(page.el("composer-error").textContent, Logic.TEXT.emptyMessage);
  assert.equal(page.el("composer-input").getAttribute("aria-invalid"), "true");
  assert.equal(page.env.posts(CONV_A).length, 0);
  page.type("а");
  await page.settle();
  assert.equal(page.el("composer-error").hidden, true);
  assert.equal(page.el("composer-input").getAttribute("aria-invalid"), null);

  page.type("😀".repeat(2001));
  assert.equal(page.el("counter").textContent, "2001 / 2000");
  assert.ok(page.el("counter").className.includes("counter-over"));
  page.el("send").click();
  await page.settle();
  assert.ok(/максимум 2000 символов, сейчас 2001/.test(page.el("composer-error").textContent));
  assert.equal(page.env.posts(CONV_A).length, 0);
  assert.equal(page.el("composer-input").value.length, 4002, "what was typed is still there");

  page.type("😀".repeat(2000));
  assert.equal(page.el("counter").textContent, "2000 / 2000");
  assert.ok(!page.el("counter").className.includes("counter-over"));
});

scenario("an_unclear_message_offers_only_retry_with_the_same_key_and_says_why_nothing_else", async () => {
  const page = await open(oneConversation());
  page.env.net.route("POST", URL_A, fail(), turnBody(CONV_A, 3, "Вопрос", 4, "Ответ"));
  await page.send("Вопрос");
  await page.settle();

  const pending = page.messages().byClass("msg-pending")[0];
  assert.deepEqual(pending.byTag("button").map((b) => b.textContent), ["Повторить"], "no way to take the text back or send it as a new one");
  assert.ok(pending.textContent.includes(Logic.TEXT.uncertainNetwork));
  assert.ok(pending.textContent.includes("Вопрос"), "the text is still there, in full");
  assert.equal(page.el("composer-input").readOnly, true);
  assert.equal(page.el("composer-hint").textContent, Logic.TEXT.hintUncertain);
  assert.ok(!page.doc.root.textContent.includes("Вернуть в поле ввода"));
  assert.equal(page.el("alerter").textContent, "");
  await page.env.timers.advance(100);
  assert.equal(page.el("alerter").textContent, Logic.TEXT.uncertainNetwork, "said aloud, once");

  page.type("Вопрос"); // typing does not unlock it: a second send is refused and makes no request
  page.el("send").click();
  await page.settle();
  assert.equal(page.env.posts(CONV_A).length, 1);

  pending.buttons("Повторить")[0].click();
  await page.settle();
  const [first, second] = page.env.posts(CONV_A);
  assert.equal(first.headers["idempotency-key"], second.headers["idempotency-key"]);
  assert.equal(page.messages().byClass("msg-pending").length, 0);
  assert.equal(page.articles().length, 4);
});

scenario("a_message_the_server_refused_can_be_taken_back_into_the_box", async () => {
  const page = await open(oneConversation());
  page.env.net.route("POST", URL_A, apiError(400, "invalid_message_text"));
  await page.send("Другой вопрос");
  await page.settle();

  const pending = page.messages().byClass("msg-pending")[0];
  assert.deepEqual(pending.byTag("button").map((b) => b.textContent), ["Вернуть в поле ввода"]);
  pending.buttons("Вернуть в поле ввода")[0].click();
  await page.settle();
  assert.equal(page.el("composer-input").value, "Другой вопрос");
  assert.equal(page.messages().byClass("msg-pending").length, 0);
  assert.equal(page.env.storage.record, null);
  assert.equal(page.doc.activeElement, page.el("composer-input"));
});

scenario("the_box_waits_for_the_history_and_says_so_and_a_failed_history_can_be_asked_again", async () => {
  const gate = deferred();
  const page = await open((env) => {
    env.signedIn([conversation(CONV_A, 0)]);
    env.net.route("GET", new RegExp(URL_A + "(\\?|$)"), async () => {
      await gate.promise;
      return historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]);
    });
  });

  assert.equal(page.el("composer-input").readOnly, true);
  assert.equal(page.el("send").getAttribute("aria-disabled"), "true");
  assert.equal(page.el("composer-hint").textContent, Logic.TEXT.historyLoading);
  page.type("Повтори");
  page.el("send").click();
  await page.settle();
  assert.equal(page.env.posts(CONV_A).length, 0);
  assert.equal(page.el("composer-input").value, "Повтори", "what was typed is not lost");

  gate.resolve();
  await page.settle();
  assert.equal(page.el("composer-input").readOnly, false);
  assert.equal(page.el("composer-hint").textContent, "");

  const failed = await open((env) => {
    env.signedIn([conversation(CONV_A, 0)]);
    env.net.route("GET", new RegExp(URL_A + "(\\?|$)"), fail(), historyBody(CONV_A, [message(1, "user", "Старый вопрос")]));
  });
  assert.equal(failed.el("composer-input").readOnly, true);
  assert.equal(failed.el("composer-hint").textContent, Logic.TEXT.historyUnavailable);
  assert.equal(failed.articles().length, 0, "not shown as an empty conversation");
  assert.equal(failed.messages().byClass("chip").length, 0);
  failed.messages().buttons("Повторить")[0].click();
  await failed.settle();
  assert.equal(failed.articles().length, 1);
  assert.equal(failed.el("composer-input").readOnly, false);
});

scenario("a_session_that_ends_takes_every_trace_of_the_conversation_off_the_page_and_late_answers_do_not_bring_it_back", async () => {
  const post = deferred();
  const page = await open((env) => {
    oneConversation()(env);
    env.net.route("GET", "/auth/me", me(30)); // the session ends 30 s after the page's clock starts
  });
  page.env.net.route("POST", URL_A, async () => {
    await post.promise;
    return turnBody(CONV_A, 3, "Секретный вопрос", 4, "Секретный ответ");
  });
  await page.send("Секретный вопрос");
  page.type("недописанное");
  assert.ok(page.root().includes("Старый вопрос") && page.root().includes("Секретный вопрос"));

  await page.env.timers.advance(29000);
  assert.equal(page.visible("screen-app"), true);
  await page.env.timers.advance(2000); // the page's own one-second tick finds the deadline
  await page.settle();

  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.visible("screen-app"), false);
  assert.equal(page.el("login-notice").textContent, Logic.TEXT.authExpired);
  const gone = ["Старый вопрос", "Старый ответ", "Секретный вопрос", "недописанное", "Разговор от"];
  for (const words of gone) assert.ok(!page.root().includes(words), "still in the page: " + words);
  assert.equal(page.el("composer-input").value, "");
  assert.equal(page.el("chat-title").textContent, "");

  post.resolve(); // the answer arrives after the end
  await page.settle();
  await page.env.timers.advance(200);
  for (const words of [...gone, "Секретный ответ"]) assert.ok(!page.root().includes(words), "came back: " + words);
  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.el("announcer").textContent, "");
  assert.equal(page.env.storage.record.turns.length, 1, "the unanswered message waits for the next sign-in");
});

scenario("a_tab_that_comes_back_checks_the_deadline_first_and_then_asks_the_server", async () => {
  const page = await open(oneConversation());
  const asks = () => page.env.net.requests("GET", "/auth/me").length;
  const before = asks();

  page.doc.visibilityState = "hidden";
  page.doc.fire("visibilitychange");
  await page.settle();
  assert.equal(asks(), before, "going away asks nothing");
  page.doc.visibilityState = "visible";
  page.doc.fire("visibilitychange");
  await page.settle();
  assert.deepEqual([asks(), page.visible("screen-app")], [before + 1, true]);

  page.env.timers.now += 3 * 86400000; // the machine slept and the page was frozen: no tick ran
  page.doc.fire("resume");
  await page.settle();
  assert.equal(asks(), before + 1, "the deadline had passed: nothing is asked");
  assert.equal(page.visible("screen-login"), true);
  assert.ok(!page.root().includes("Старый вопрос"));
});

scenario("a_failed_message_in_the_history_can_be_retried_with_its_own_key", async () => {
  const page = await open(oneConversation([message(3, "user", "Не вышло", { status: "failed", idempotency_key: "web-k3", error_code: "provider_error" })]));
  page.env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Не вышло", 4, "Теперь вышло"));

  const failed = page.messages().byClass("msg-failed")[0];
  assert.ok(failed.textContent.includes(Logic.CODE_MESSAGES.provider_error));
  failed.buttons("Повторить")[0].click();
  await page.settle();

  const [post] = page.env.posts(CONV_A);
  assert.equal(post.headers["idempotency-key"], "web-k3");
  assert.deepEqual(post.body, { text: "Не вышло" });
  assert.equal(page.messages().byClass("msg-failed").length, 0);
  assert.ok(page.articleTexts().some((t) => t.includes("Теперь вышло")));
});

scenario("a_5xx_is_not_a_verdict_it_shows_its_number_and_offers_only_retry_and_a_certain_refusal_offers_a_new_message", async () => {
  const failure = apiError(502, "provider_unreachable", { retryable: true });
  const page = await open(oneConversation());
  page.env.net.route("POST", URL_A, failure, apiError(409, "turn_superseded"));
  await page.send("Вопрос");
  await page.settle();

  let pending = page.messages().byClass("msg-pending")[0];
  assert.ok(pending.textContent.includes(Logic.TEXT.uncertainServer));
  assert.ok(pending.textContent.includes("Номер обращения: " + failure.headers["X-Correlation-ID"]));
  assert.deepEqual(pending.byTag("button").map((b) => b.textContent), ["Повторить"], "no way to take the text back or send it as new");
  assert.ok(!page.root().includes("Вернуть в поле ввода"));
  assert.equal(page.el("composer-input").readOnly, true);
  pending.buttons("Повторить")[0].click();
  await page.settle();
  pending = page.messages().byClass("msg-pending")[0];
  assert.deepEqual(pending.byTag("button").map((b) => b.textContent), ["Отправить как новое сообщение", "Вернуть в поле ввода"]);
  assert.equal(page.env.posts(CONV_A).length, 2);
});

// ============================================================================================
// Navigation, the drawer, signing out
// ============================================================================================

scenario("the_drawer_opens_and_closes_on_a_narrow_screen_and_does_nothing_on_a_wide_one", async () => {
  const narrow = await open(oneConversation(), { narrow: true });
  const app = narrow.el("screen-app");
  const toggle = narrow.el("drawer-toggle");

  assert.equal(app.getAttribute("data-drawer"), "closed");
  toggle.click();
  assert.deepEqual([app.getAttribute("data-drawer"), toggle.getAttribute("aria-expanded"), narrow.el("backdrop").hidden, narrow.el("chat").inert],
    ["open", "true", false, true]);
  assert.equal(narrow.doc.activeElement, narrow.el("tab-chats"), "the drawer opens on the part that is in front");
  narrow.doc.fire("keydown", { key: "Escape" });
  assert.deepEqual([app.getAttribute("data-drawer"), toggle.getAttribute("aria-expanded"), narrow.el("backdrop").hidden, narrow.el("chat").inert],
    ["closed", "false", true, false]);
  assert.equal(narrow.doc.activeElement, toggle);

  toggle.click();
  narrow.el("backdrop").click();
  assert.equal(app.getAttribute("data-drawer"), "closed");
  toggle.click();
  narrow.el("conversation-list").byTag("button")[0].click();
  await narrow.settle();
  assert.equal(app.getAttribute("data-drawer"), "closed", "choosing a conversation closes it");
  assert.equal(narrow.doc.activeElement, narrow.el("chat-title"));

  const wide = await open(oneConversation(), { narrow: false });
  wide.el("drawer-toggle").click();
  assert.equal(wide.el("screen-app").getAttribute("data-drawer"), "closed");
});

scenario("a_new_conversation_is_a_draft_with_the_box_focused_and_nothing_is_created", async () => {
  const page = await open(oneConversation());
  page.el("new-conversation").click();
  await page.settle();

  assert.equal(page.el("chat-title").textContent, "Новый разговор");
  assert.equal(page.el("new-conversation").getAttribute("aria-current"), "true");
  assert.equal(page.doc.activeElement, page.el("composer-input"));
  assert.equal(page.articles().length, 0);
  assert.ok(page.messages().byClass("chip").length === 3);
  assert.equal(page.env.net.requests("POST", /.*/).length, 0);

  page.messages().byClass("chip")[1].click(); // a suggestion fills the box and sends nothing
  assert.equal(page.el("composer-input").value, "Как приготовить борщ?");
  assert.equal(page.env.net.requests("POST", /.*/).length, 0);
});

// The menu of the open conversation, and the one action it offers today.
function openActions(page) {
  page.el("chat-actions-toggle").click();
  return page.el("chat-actions-menu");
}

function chooseDelete(page) {
  const menu = openActions(page);
  menu.byTag("button").find((button) => button.textContent === Logic.TEXT.deleteNamed).click();
}

scenario("the_actions_menu_is_offered_only_for_the_saved_conversation_that_is_open", async () => {
  const page = await open(oneConversation());
  const toggle = page.el("chat-actions-toggle");

  assert.equal(page.visible("chat-actions"), true, "a saved conversation has actions");
  assert.equal(toggle.getAttribute("aria-haspopup"), "menu");
  assert.equal(toggle.getAttribute("aria-controls"), "chat-actions-menu");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(toggle.getAttribute("aria-label"), "Действия с разговором");
  assert.equal(page.visible("chat-actions-menu"), false, "the menu is closed until it is asked for");

  const menu = openActions(page);
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.equal(page.visible("chat-actions-menu"), true);
  assert.equal(menu.getAttribute("role"), "menu");
  assert.equal(menu.getAttribute("aria-label"), "Действия с разговором");
  const items = menu.byTag("button");
  assert.deepEqual(items.map((button) => button.textContent), [Logic.TEXT.deleteNamed]);
  assert.equal(items[0].getAttribute("role"), "menuitem");
  assert.equal(items[0].getAttribute("tabindex"), "-1");
  assert.equal(page.doc.activeElement, items[0], "the first action is focused when the menu opens");

  page.el("tab-favorites").click();
  await page.settle();
  assert.equal(page.visible("chat-actions"), false, "the favorites have no conversation actions");
  assert.equal(page.visible("chat-actions-menu"), false);
  page.el("tab-chats").click();
  await page.settle();
  assert.equal(page.visible("chat-actions"), true);

  page.el("new-conversation").click();
  await page.settle();
  assert.equal(page.visible("chat-actions"), false, "a new draft has no actions");
  assert.equal(page.visible("chat-actions-menu"), false);
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
});

scenario("opening_another_conversation_closes_the_actions_menu", async () => {
  const page = await open(
    signedInWith([conversation(CONV_B, 30, "Суп"), conversation(CONV_A, 0, "Борщ")], {
      [CONV_B]: historyBody(CONV_B, [message(5, "user", "Про суп")]),
      [CONV_A]: historyBody(CONV_A, [message(6, "user", "Про борщ")]),
    })
  );
  const toggle = page.el("chat-actions-toggle");

  openActions(page);
  assert.equal(page.visible("chat-actions-menu"), true);
  page.el("conversation-list").byTag("button")[1].click();
  await page.settle();

  assert.equal(page.el("chat-title").textContent, "Борщ");
  assert.equal(page.visible("chat-actions"), true, "the newly opened conversation has its own actions");
  assert.equal(page.visible("chat-actions-menu"), false, "the menu of the first one is gone");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
});

function outsideApplication(page, id) {
  for (let node = page.el(id); node; node = node.parentNode) {
    if (node === page.el("screen-app")) return false;
  }
  return true;
}

// A key press as a browser delivers it: to the node the key happened on, then up through each
// of its ancestors, then to the document. The mini DOM (fake_dom.js) has no bubbling of its
// own, so the scenarios model it here; the event keeps the node the key happened on as its
// target, and the same event object is handed to every listener on the way.
function pressKey(page, target, init = {}) {
  const event = {
    type: "keydown",
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    ...init,
    target,
  };
  const path = [];
  for (let node = target; node; node = node.parentNode) {
    path.push(node);
  }
  path.push(page.doc);
  for (const node of path) {
    const listeners = (node.listeners && node.listeners.keydown) || [];
    for (const listener of listeners.slice()) {
      listener.call(node, event);
    }
  }
  return event;
}

scenario("the_help_explanation_opens_in_a_modal_over_an_active_conversation_and_changes_nothing", async () => {
  const page = await open(oneConversation());
  const toggle = page.el("help-toggle");
  const panel = page.el("help-panel");
  const backdrop = page.el("help-backdrop");
  const close = page.el("help-close");
  const app = page.el("screen-app");

  // An active conversation (two messages), an unsent draft and a scrolled list: the modal
  // must leave all three exactly as they are.
  assert.equal(page.articles().length, 2);
  const before = page.articleTexts();
  assert.ok(before.some((text) => text.includes("Старый вопрос")));
  page.type("неотправленное сообщение");
  page.messages().scrollTop = 120;

  // Closed until asked for; the entry is in the conversation header.
  assert.equal(page.visible("help-panel"), false);
  assert.equal(page.visible("help-backdrop"), false);
  assert.equal(app.inert, false);
  assert.equal(toggle.parentNode.className, "chat-head", "the entry is in the conversation header");
  assert.equal(toggle.getAttribute("type"), "button");
  assert.equal(toggle.getAttribute("aria-haspopup"), "dialog");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(toggle.getAttribute("aria-controls"), "help-panel");
  assert.equal(toggle.getAttribute("aria-label"), "Помощь");
  assert.equal(panel.getAttribute("role"), "dialog");
  assert.equal(panel.getAttribute("aria-modal"), "true");
  assert.equal(panel.getAttribute("aria-labelledby"), "help-title");
  assert.equal(panel.getAttribute("aria-describedby"), "help-text");
  assert.ok(outsideApplication(page, "help-panel"), "the dialog is outside the box made inert");

  toggle.click();
  assert.equal(page.visible("help-panel"), true);
  assert.equal(page.visible("help-backdrop"), true);
  assert.equal(app.inert, true, "the page behind the dialog takes no interaction");
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.equal(page.doc.activeElement, close, "the focus moves into the dialog");
  assert.equal(page.el("help-title").textContent, Logic.TEXT.helpTitle);
  const text = page.el("help-text").textContent;
  assert.equal(text, Logic.TEXT.help);
  for (const phrase of ["идеи блюд", "полный рецепт", "избранное"]) {
    assert.ok(text.includes(phrase), "the help explains " + phrase);
  }
  // The conversation is neither cleared nor changed, the draft stays, the list stays where
  // it was, and no request is made.
  assert.deepEqual(page.articleTexts(), before);
  assert.equal(page.el("composer-input").value, "неотправленное сообщение");
  assert.equal(page.messages().scrollTop, 120);
  assert.equal(page.env.net.requests("POST", /.*/).length, 0);

  // The focus never leaves the dialog: Tab and Shift+Tab keep it on the only control. The key
  // happens on the focused element and reaches the page's handling through the document.
  const tab = pressKey(page, close, { key: "Tab" });
  assert.equal(tab.defaultPrevented, true);
  assert.equal(page.doc.activeElement, close);
  const back = pressKey(page, close, { key: "Tab", shiftKey: true });
  assert.equal(back.defaultPrevented, true);
  assert.equal(page.doc.activeElement, close);

  // Escape closes it, gives the focus back to the entry and leaves everything as it was.
  const escaped = pressKey(page, close, { key: "Escape" });
  assert.equal(escaped.defaultPrevented, true);
  assert.equal(page.visible("help-panel"), false);
  assert.equal(page.visible("help-backdrop"), false);
  assert.equal(app.inert, false);
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(page.doc.activeElement, toggle);
  assert.deepEqual(page.articleTexts(), before);
  assert.equal(page.el("composer-input").value, "неотправленное сообщение");
  assert.equal(page.messages().scrollTop, 120);

  // The close button and a click outside the dialog both close it the same way.
  for (const dismiss of [() => close.click(), () => backdrop.click()]) {
    toggle.click();
    assert.equal(page.visible("help-panel"), true);
    dismiss();
    assert.equal(page.visible("help-panel"), false);
    assert.equal(page.visible("help-backdrop"), false);
    assert.equal(app.inert, false);
    assert.equal(page.doc.activeElement, toggle, "the focus returns to the button that asked");
    assert.deepEqual(page.articleTexts(), before);
  }
});

scenario("the_help_modal_hears_the_keyboard_wherever_the_focus_is_and_only_while_it_is_open", async () => {
  const page = await open(oneConversation());
  const toggle = page.el("help-toggle");
  const close = page.el("help-close");
  const body = page.doc.body;
  const app = page.el("screen-app");
  const handlers = page.doc.listeners.keydown.length;

  // Escape that really happens on the page body (the focus thrown out of the dialog by a
  // script, an extension or a browser quirk) still closes it and gives the focus back.
  toggle.click();
  assert.equal(page.visible("help-panel"), true);
  assert.equal(page.doc.activeElement, close);
  page.doc.activeElement = body;
  const escaped = pressKey(page, body, { key: "Escape" });
  assert.equal(escaped.target, body, "the key really happens outside the dialog");
  assert.equal(escaped.defaultPrevented, true);
  assert.equal(page.visible("help-panel"), false);
  assert.equal(page.visible("help-backdrop"), false);
  assert.equal(app.inert, false);
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(page.doc.activeElement, toggle, "the focus returns to the entry");

  // Tab and Shift+Tab from outside pull the focus back to the edge of the dialog: the first
  // control for Tab, the last for Shift+Tab (one control here, so both land on it).
  for (const shiftKey of [false, true]) {
    toggle.click();
    assert.equal(page.visible("help-panel"), true);
    page.doc.activeElement = body;
    const tab = pressKey(page, body, { key: "Tab", shiftKey });
    assert.equal(tab.defaultPrevented, true);
    assert.equal(page.doc.activeElement, close, "the focus is back inside the dialog");
    assert.equal(page.visible("help-panel"), true, "the dialog stays open");
    pressKey(page, close, { key: "Escape" });
    assert.equal(page.visible("help-panel"), false);
  }

  // Closed, the page's keys are its own again: nothing is prevented, the focus does not move.
  page.doc.activeElement = body;
  const closedTab = pressKey(page, body, { key: "Tab" });
  assert.equal(closedTab.defaultPrevented, false);
  assert.equal(page.doc.activeElement, body);
  const closedEscape = pressKey(page, body, { key: "Escape" });
  assert.equal(closedEscape.defaultPrevented, false);
  assert.equal(page.visible("help-panel"), false);

  // Repeated opening and closing adds no handler and leaves none behind.
  for (let i = 0; i < 6; i++) {
    toggle.click();
    assert.equal(page.visible("help-panel"), true);
    close.click();
    assert.equal(page.visible("help-panel"), false);
  }
  assert.equal(page.doc.listeners.keydown.length, handlers, "one dialog keydown handler, never duplicated");

  // The session ending under an open dialog changes the screen as it did before: the dialog
  // closes, the login takes the focus, and its keys are not heard any more.
  const ended = await open((env) => {
    oneConversation()(env);
    env.net.route("GET", "/auth/me", me(30));
  });
  ended.el("help-toggle").click();
  assert.equal(ended.visible("help-panel"), true);
  await ended.env.timers.advance(31000);
  await ended.settle();
  assert.equal(ended.visible("screen-login"), true);
  assert.equal(ended.visible("help-panel"), false);
  assert.equal(ended.el("screen-app").inert, false);
  assert.equal(ended.doc.activeElement, ended.el("login-title"));
  const lateEscape = pressKey(ended, ended.doc.body, { key: "Escape" });
  assert.equal(lateEscape.defaultPrevented, false);
});

scenario("the_help_explanation_is_reachable_in_an_empty_conversation_on_a_narrow_screen", async () => {
  const page = await open(oneConversation(), { narrow: true });
  page.el("new-conversation").click();
  await page.settle();

  // A new draft is an empty conversation, and the entry is still there.
  assert.equal(page.articles().length, 0);
  assert.equal(page.visible("help-toggle"), true);
  page.el("help-toggle").click();
  assert.equal(page.visible("help-panel"), true);
  assert.equal(page.visible("help-backdrop"), true);
  assert.equal(page.el("help-text").textContent, Logic.TEXT.help);
  assert.equal(page.articles().length, 0, "the explanation did not create a message");
  assert.equal(page.el("screen-app").inert, true);

  // Closing gives the focus back to the entry.
  page.el("help-close").click();
  assert.equal(page.visible("help-panel"), false);
  assert.equal(page.el("screen-app").inert, false);
  assert.equal(page.doc.activeElement, page.el("help-toggle"));
});

scenario("the_actions_menu_is_operable_from_the_keyboard_and_dismissed_by_an_outside_click", async () => {
  const page = await open(oneConversation());
  const toggle = page.el("chat-actions-toggle");
  const menu = page.el("chat-actions-menu");

  toggle.fire("keydown", { key: "ArrowDown" });
  assert.equal(page.visible("chat-actions-menu"), true);
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  const item = menu.byTag("button")[0];
  assert.equal(page.doc.activeElement, item, "ArrowDown opens the menu on its first action");

  item.fire("keydown", { key: "ArrowDown" });
  assert.equal(page.doc.activeElement, item, "the last action wraps to the first (there is one)");
  item.fire("keydown", { key: "ArrowUp" });
  assert.equal(page.doc.activeElement, item);
  item.fire("keydown", { key: "Home" });
  assert.equal(page.doc.activeElement, item);
  item.fire("keydown", { key: "End" });
  assert.equal(page.doc.activeElement, item);
  item.fire("keydown", { key: "Escape" });
  assert.equal(page.visible("chat-actions-menu"), false);
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(page.doc.activeElement, toggle, "Escape gives the focus back to the actions button");

  toggle.click();
  assert.equal(page.doc.activeElement, item);
  page.doc.fire("click", { target: page.el("messages") });
  assert.equal(page.visible("chat-actions-menu"), false, "a click elsewhere calls the menu off");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");

  toggle.click();
  item.fire("keydown", { key: "Tab" });
  assert.equal(page.visible("chat-actions-menu"), false, "Tab leaves the menu and closes it");

  toggle.click();
  page.el("chat-actions").fire("focusout", { relatedTarget: page.el("messages") });
  assert.equal(page.visible("chat-actions-menu"), false, "the focus moving out closes it");
});

scenario("deleting_the_open_conversation_asks_first_and_returns_to_a_new_empty_one", async () => {
  const page = await open(oneConversation(), { narrow: true });
  page.env.net.route("DELETE", `/api/conversations/${CONV_A}`, H.removedBody());
  // The list the server returns after the deletion: the page reads it again by itself.
  page.env.net.route("GET", "/api/conversations", listBody([]));
  const toggle = page.el("chat-actions-toggle");
  const confirmation = page.el("chat-delete");

  chooseDelete(page);
  await page.settle();
  let question = confirmation.byClass("confirm")[0];
  assert.ok(question, "choosing the action asks the same question as before");
  assert.equal(page.visible("chat-delete"), true);
  assert.equal(page.visible("chat-actions-menu"), false, "choosing the action closes the menu");
  assert.ok(question.textContent.includes(Logic.TEXT.deleteAsk));
  assert.deepEqual(question.byTag("button").map((button) => button.textContent), [
    Logic.TEXT.deleteConfirm,
    Logic.TEXT.deleteCancel,
  ]);
  assert.equal(page.doc.activeElement.textContent, Logic.TEXT.deleteCancel, "the safe answer is focused");
  assert.equal(page.env.net.requests("DELETE", /.*/).length, 0, "asking removes nothing");

  question.buttons(Logic.TEXT.deleteCancel)[0].click();
  await page.settle();
  assert.equal(page.env.net.requests("DELETE", /.*/).length, 0);
  assert.equal(page.visible("chat-delete"), false, "cancelling puts the question away");
  assert.equal(page.doc.activeElement, toggle, "the focus goes back to the actions button");
  assert.equal(page.visible("chat-actions"), true);

  chooseDelete(page);
  await page.settle();
  confirmation.byClass("confirm")[0].buttons(Logic.TEXT.deleteConfirm)[0].click();
  await page.settle();

  assert.equal(page.env.net.requests("DELETE", /.*/).length, 1);
  assert.equal(page.env.net.requests("GET", "/api/conversations").length, 2, "the list is read again");
  assert.equal(page.el("conversation-list").childNodes.length, 0);
  assert.equal(page.el("list-status").textContent, "Разговоров пока нет. Начните новый.");
  assert.equal(page.el("chat-title").textContent, "Новый разговор");
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "closed", "the drawer closes on the new draft");
  assert.ok(page.doc.activeElement === page.el("chat-title"));
  assert.equal(page.articles().length, 0);
  assert.equal(page.el("banner-text").textContent, Logic.TEXT.deleted);
  assert.equal(page.visible("banner"), true);
  assert.equal(page.visible("chat-actions"), false, "the new draft has no actions");
  assert.equal(page.visible("chat-delete"), false);
  assert.equal(page.el("composer-input").readOnly, false, "the new conversation can be written to");
  await page.env.timers.advance(100);
  assert.equal(page.el("announcer").textContent, Logic.TEXT.deleted);
});

scenario("the_actions_menu_deletes_the_open_conversation_and_the_list_keeps_the_rest", async () => {
  const page = await open(
    signedInWith([conversation(CONV_B, 30, "Суп"), conversation(CONV_A, 0, "Борщ")], {
      [CONV_B]: historyBody(CONV_B, [message(5, "user", "Про суп"), message(6, "assistant", "Суп так")]),
      [CONV_A]: historyBody(CONV_A, [message(6, "user", "Про борщ")]),
    })
  );
  page.env.net.route("DELETE", `/api/conversations/${CONV_B}`, H.removedBody());
  page.env.net.route("GET", "/api/conversations", listBody([conversation(CONV_A, 0, "Борщ")]));
  const list = page.el("conversation-list");

  chooseDelete(page);
  await page.settle();
  page.el("chat-delete").byClass("confirm")[0].buttons(Logic.TEXT.deleteConfirm)[0].click();
  await page.settle();

  assert.equal(page.env.net.requests("DELETE", /.*/).length, 1);
  assert.equal(page.env.net.requests("DELETE", new RegExp(CONV_A)).length, 0, "nothing but the open conversation was asked for");
  assert.deepEqual(list.byTag("button").map((button) => button.textContent.includes("Борщ")), [true]);
  assert.ok(list.textContent.includes("Борщ"));
  assert.equal(page.el("chat-title").textContent, "Новый разговор");
  assert.equal(page.visible("chat-actions"), false, "the new draft has no actions");
  assert.ok(page.doc.activeElement === page.el("chat-title"));

  list.byTag("button")[0].click();
  await page.settle();
  assert.equal(page.el("chat-title").textContent, "Борщ");
  assert.equal(page.visible("chat-actions"), true, "the remaining conversation has its own actions");
});

scenario("a_failed_deletion_keeps_the_conversation_and_offers_a_retry", async () => {
  const page = await open(oneConversation());
  const failure = apiError(503, "service_unavailable");
  page.env.net.route("DELETE", `/api/conversations/${CONV_A}`, failure, H.removedBody());
  page.env.net.route("GET", "/api/conversations", listBody([]));
  const title = page.el("chat-title").textContent;

  chooseDelete(page);
  await page.settle();
  page.el("chat-delete").byClass("confirm")[0].buttons(Logic.TEXT.deleteConfirm)[0].click();
  await page.settle();

  let question = page.el("chat-delete").byClass("confirm")[0];
  assert.ok(question.textContent.includes(failure.body.message), "the reason is shown where the question was");
  assert.ok(question.textContent.includes("Номер обращения: " + failure.headers["X-Correlation-ID"]));
  assert.deepEqual(question.byTag("button").map((button) => button.textContent), [
    Logic.TEXT.retry,
    Logic.TEXT.deleteCancel,
  ]);
  assert.equal(page.doc.activeElement.textContent, Logic.TEXT.retry, "the retry takes the focus");
  assert.equal(page.el("chat-title").textContent, title, "the conversation is still open");
  assert.equal(page.visible("banner"), false);
  assert.equal(page.visible("chat-actions"), true);
  assert.equal(page.env.net.requests("GET", "/api/conversations").length, 1, "a failed deletion does not refresh the list");

  page.el("chat-delete").byClass("confirm")[0].buttons(Logic.TEXT.retry)[0].click();
  await page.settle();
  assert.equal(page.env.net.requests("DELETE", /.*/).length, 2);
  assert.equal(page.el("chat-title").textContent, "Новый разговор");
  assert.equal(page.el("banner-text").textContent, Logic.TEXT.deleted);
});

scenario("a_failed_list_refresh_after_a_deletion_offers_its_retry_and_keeps_it_deleted", async () => {
  const page = await open(oneConversation());
  page.env.net.route("DELETE", `/api/conversations/${CONV_A}`, H.removedBody());
  page.env.net.route("GET", "/api/conversations", fail(), listBody([]));
  const list = page.el("conversation-list");

  chooseDelete(page);
  await page.settle();
  page.el("chat-delete").byClass("confirm")[0].buttons(Logic.TEXT.deleteConfirm)[0].click();
  await page.settle();

  assert.equal(page.el("banner-text").textContent, Logic.TEXT.deleted, "the deletion itself is confirmed");
  assert.equal(page.visible("list-retry"), true, "the failed refresh can be asked for again");
  assert.equal(page.el("list-status").textContent, Logic.TEXT.network);
  assert.equal(list.childNodes.length, 0, "the failure does not put the deleted conversation back");

  page.el("list-retry").click();
  await page.settle();
  assert.equal(page.visible("list-retry"), false);
  assert.equal(page.el("list-status").textContent, "Разговоров пока нет. Начните новый.");
  assert.equal(list.childNodes.length, 0);
  assert.equal(page.env.net.requests("GET", "/api/conversations").length, 3);
});

scenario("the_actions_menu_works_on_a_narrow_screen_after_the_drawer_closes", async () => {
  const page = await open(oneConversation(), { narrow: true });
  page.env.net.route("DELETE", `/api/conversations/${CONV_A}`, H.removedBody());
  page.env.net.route("GET", "/api/conversations", listBody([]));
  const toggle = page.el("chat-actions-toggle");

  openActions(page);
  assert.equal(page.visible("chat-actions-menu"), true);
  page.el("drawer-toggle").click();
  assert.equal(page.visible("chat-actions-menu"), false, "opening the drawer closes the menu it covers");
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "open");

  page.el("conversation-list").byTag("button")[0].click();
  await page.settle();
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "closed", "choosing a conversation closes the drawer");
  assert.equal(page.visible("chat-actions"), true);

  chooseDelete(page);
  await page.settle();
  assert.equal(page.visible("chat-delete"), true, "the question is in the chat, not in the closed drawer");
  page.el("chat-delete").byClass("confirm")[0].buttons(Logic.TEXT.deleteConfirm)[0].click();
  await page.settle();

  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "closed");
  assert.equal(page.el("chat-title").textContent, "Новый разговор");
  assert.equal(page.visible("chat-actions"), false);
  assert.ok(page.doc.activeElement === page.el("chat-title"));
});

scenario("signing_out_leaves_nothing_of_the_conversation_on_screen_or_in_storage", async () => {
  const page = await open(oneConversation());
  page.env.net.route("POST", URL_A, fail());
  await page.send("Вопрос");
  await page.settle();
  assert.ok(page.env.storage.record);
  page.type("недописанное");

  page.env.net.route("POST", "/auth/logout", text(204, null));
  page.el("logout").click();
  await page.settle();

  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.visible("screen-app"), false);
  assert.equal(page.el("login-notice").textContent, Logic.TEXT.loggedOut);
  assert.equal(page.el("messages").childNodes.length, 0);
  assert.equal(page.el("conversation-list").childNodes.length, 0);
  assert.equal(page.el("composer-input").value, "");
  assert.equal(page.env.storage.record, null);
  assert.equal(page.el("logout").getAttribute("aria-disabled"), "false");
});

scenario("a_session_that_ends_mid_conversation_shows_the_login_with_the_reason_and_clears_the_screen", async () => {
  const page = await open(oneConversation());
  page.env.net.route("POST", URL_A, notAuthenticated());
  await page.send("Вопрос");
  await page.settle();

  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.el("login-notice").textContent, Logic.TEXT.authExpired);
  assert.ok(page.el("login-notice").className.includes("notice-error"));
  assert.equal(page.el("messages").childNodes.length, 0);
  assert.equal(page.el("composer-input").value, "");
  assert.equal(page.env.storage.record.turns.length, 1, "the unsent message is kept for after the next sign-in");
});

scenario("a_page_restored_from_the_back_forward_cache_asks_again_who_is_signed_in", async () => {
  const page = await open(oneConversation());
  assert.equal(page.visible("screen-app"), true);
  const asked = page.env.net.requests("GET", "/auth/me").length;

  page.fireWindow("pageshow", { persisted: false }); // an ordinary load: nothing more to ask
  await page.settle();
  assert.equal(page.env.net.requests("GET", "/auth/me").length, asked);

  page.env.net.route("GET", "/auth/me", notAuthenticated()); // signed out since the page was left
  page.fireWindow("pageshow", { persisted: true });
  await page.settle();

  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.visible("screen-app"), false);
  assert.equal(page.el("messages").childNodes.length, 0);
  assert.equal(page.el("conversation-list").childNodes.length, 0);
  assert.equal(page.el("login-notice").hidden, true, "a definite 401 on asking again is a plain sign-in, not an 'expired' complaint");
});

scenario("a_sign_out_whose_answer_is_lost_leaves_the_conversation_and_says_it_is_not_sure_and_offers_to_try_again", async () => {
  const reply = deferred();
  const page = await open(oneConversation());
  page.env.net.route("POST", URL_A, async () => {
    await reply.promise;
    return turnBody(CONV_A, 3, "Секретный вопрос", 4, "Секретный поздний ответ");
  });
  page.type("Секретный вопрос");
  page.el("send").click();
  await page.settle(); // (the answer is still on its way)
  page.type("недописанное");
  assert.ok(page.root().includes("Старый вопрос") && page.root().includes("Секретный вопрос"));

  page.env.net.route("POST", "/auth/logout", fail()); // the server may have revoked it; the answer did not arrive
  page.el("logout").click();
  await page.settle();

  // Not the signed-in page, and not the login screen either: the page does not know the session is closed.
  assert.equal(page.visible("screen-app"), false);
  assert.equal(page.visible("screen-login"), false);
  assert.equal(page.visible("screen-unavailable"), true);
  assert.equal(page.el("unavailable-text").textContent, Logic.TEXT.logoutUnconfirmed);
  assert.equal(page.el("unavailable-retry").textContent, Logic.TEXT.logoutAgain);
  assert.ok(!page.root().includes("Вы вышли"), "it does not claim the session is closed");
  const gone = ["Старый вопрос", "Старый ответ", "Секретный вопрос", "недописанное", "Разговор от"];
  for (const words of gone) assert.ok(!page.root().includes(words), "still in the page: " + words);
  assert.equal(page.el("composer-input").value, "");
  assert.equal(page.env.storage.record, null);

  reply.resolve(); // the answer to the message that was on its way arrives
  await page.settle();
  await page.env.timers.advance(200);
  for (const words of [...gone, "Секретный поздний ответ"]) assert.ok(!page.root().includes(words), "came back: " + words);
  assert.equal(page.visible("screen-unavailable"), true);
  assert.equal(page.el("announcer").textContent, "");

  // The button leaves again; this time the server answers.
  page.env.net.route("POST", "/auth/logout", text(204, null));
  page.el("unavailable-retry").click();
  await page.settle();
  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.el("login-notice").textContent, Logic.TEXT.loggedOut);
  assert.equal(page.visible("screen-unavailable"), false);
  const asked = page.env.net.requests("POST", "/auth/logout");
  assert.deepEqual(asked.map((r) => r.headers["x-expected-user"]), [H.USER_A, H.USER_A], "both tries name the person who was signed in");
});

scenario("a_repeated_sign_out_that_finds_somebody_else_signed_in_says_so_and_leaves_them_alone", async () => {
  const page = await open(oneConversation());
  page.type("недописанное");
  page.env.net.route("POST", "/auth/logout", fail()); // the server ended her session; the answer was lost
  page.el("logout").click();
  await page.settle();
  assert.equal(page.el("unavailable-retry").textContent, Logic.TEXT.logoutAgain);

  // Boris signed in, in another tab of the same browser; the server finds his session in the cookie of her retry.
  const boris = [message(7, "user", "Про суп Бориса"), message(8, "assistant", "Суп так")];
  page.env.net.route("GET", "/auth/me", me(86400, H.USER_B));
  page.env.net.route("GET", "/api/conversations", listBody([conversation(CONV_B, 0)]));
  page.env.net.route("GET", new RegExp("/api/conversations/" + CONV_B + "/messages(\\?|$)"), historyBody(CONV_B, boris));
  page.env.net.route("POST", "/auth/logout", json(409, { detail: "session_mismatch" }));
  page.el("unavailable-retry").click();
  await page.settle();

  assert.equal(page.visible("screen-unavailable"), true);
  assert.equal(page.visible("screen-app"), false);
  assert.equal(page.visible("screen-login"), false);
  assert.equal(page.el("unavailable-text").textContent, Logic.TEXT.logoutMismatch);
  assert.equal(page.el("unavailable-retry").textContent, Logic.TEXT.retry, "it does not offer to leave again");
  assert.ok(!page.root().includes("Вы вышли"), "it does not claim that anything was closed");
  for (const words of ["Старый вопрос", "Старый ответ", "недописанное", "Разговор от", "Про суп Бориса"]) {
    assert.ok(!page.root().includes(words), "on the page: " + words);
  }
  assert.equal(page.env.storage.record, null);
  assert.equal(page.env.net.requests("POST", "/auth/logout").length, 2);
  await page.env.timers.advance(120000);
  assert.equal(page.env.net.requests("POST", "/auth/logout").length, 2, "nobody is signed out later either");

  // The button asks who is signed in, from the beginning: Boris's page, none of hers.
  page.el("unavailable-retry").click();
  await page.settle();
  assert.equal(page.visible("screen-app"), true);
  assert.equal(page.visible("screen-unavailable"), false);
  assert.ok(page.articleTexts().some((text) => text.includes("Про суп Бориса")), "his conversation is on the page");
  for (const words of ["Старый вопрос", "Старый ответ", "недописанное"]) {
    assert.ok(!page.root().includes(words), "came back: " + words);
  }
  const asked = page.env.net.requests("POST", "/auth/logout");
  assert.deepEqual(asked.map((r) => r.headers["x-expected-user"]), [H.USER_A, H.USER_A]);
});

// The page is held back whenever it asks who is signed in; the draft is kept for the same person only.
const paused = () => {
  const gate = deferred();
  return {
    gate,
    answer: (reply) => async () => {
      await gate.promise;
      return reply;
    },
  };
};
const comesBack = async (page) => {
  page.doc.visibilityState = "visible";
  page.doc.fire("visibilitychange");
};

scenario("a_tab_that_comes_back_is_held_back_until_the_server_says_who_is_there_and_the_same_person_gets_the_draft_back", async () => {
  const page = await open(oneConversation());
  page.type("недописанное");
  const wait = paused();
  page.env.net.route("GET", "/auth/me", wait.answer(me()));

  await comesBack(page);
  await flush(); // (the question is out; nothing has been answered)
  assert.equal(page.visible("screen-app"), false, "hidden at once, before any answer");
  assert.equal(page.visible("screen-loading"), true);
  assert.equal(page.el("loading-text").textContent, Logic.TEXT.verifying);
  for (const words of ["Старый вопрос", "Старый ответ", "недописанное", "Разговор от"]) {
    assert.ok(!page.root().includes(words), "in the page while asking: " + words);
  }
  assert.equal(page.el("composer-input").value, "");

  wait.gate.resolve();
  await page.settle();
  assert.equal(page.visible("screen-app"), true);
  assert.equal(page.visible("screen-loading"), false);
  assert.ok(page.articleTexts().some((t) => t.includes("Старый вопрос")));
  assert.equal(page.el("composer-input").value, "недописанное", "the same person: the draft comes back");
  assert.equal(page.el("counter").textContent, "12 / 2000");
});

scenario("a_tab_that_comes_back_to_another_person_shows_nothing_of_the_last_one_not_even_the_draft", async () => {
  const page = await open(oneConversation());
  page.env.net.route("POST", URL_A, fail());
  await page.send("Недосланное Анны"); // (the connection drops: an unresolved message)
  page.type("черновик Анны");
  const wait = paused();
  page.env.net.route("GET", "/auth/me", wait.answer(me(86400, H.USER_B)));
  page.env.net.route("GET", "/api/conversations", listBody([conversation(CONV_B, 0)]));
  page.env.net.route("GET", new RegExp("/api/conversations/" + CONV_B + "/messages(\\?|$)"), historyBody(CONV_B, [message(7, "user", "Про суп Бориса"), message(8, "assistant", "Суп так")]));

  await comesBack(page);
  await flush();
  assert.equal(page.visible("screen-app"), false);
  wait.gate.resolve();
  await page.settle();

  assert.equal(page.visible("screen-app"), true);
  assert.ok(page.articleTexts().some((t) => t.includes("Про суп Бориса")));
  for (const words of ["Старый вопрос", "Старый ответ", "Недосланное Анны", "черновик Анны"]) {
    assert.ok(!page.root().includes(words), "still in the page: " + words);
  }
  assert.equal(page.el("composer-input").value, "", "her draft is not his");
  assert.equal(page.el("counter").textContent, "");
  assert.equal(page.env.storage.record, null);
});

scenario("a_tab_that_cannot_find_out_who_is_there_stays_hidden_says_so_and_asks_again_on_request", async () => {
  const page = await open(oneConversation());
  page.type("недописанное");
  page.env.net.route("GET", "/auth/me", fail());

  await comesBack(page);
  await page.settle();
  assert.equal(page.visible("screen-app"), false);
  assert.equal(page.visible("screen-unavailable"), true);
  assert.ok(page.el("unavailable-text").textContent.startsWith(Logic.TEXT.unverified));
  assert.equal(page.el("unavailable-retry").textContent, Logic.TEXT.retry);
  assert.equal(page.visible("screen-login"), false, "it does not say anyone is signed out");
  for (const words of ["Старый вопрос", "Старый ответ", "недописанное", "Разговор от"]) {
    assert.ok(!page.root().includes(words), "in the page: " + words);
  }

  page.env.net.route("GET", "/auth/me", me());
  page.el("unavailable-retry").click();
  await page.settle();
  assert.equal(page.visible("screen-app"), true);
  assert.equal(page.el("composer-input").value, "недописанное");
  assert.ok(page.articleTexts().some((t) => t.includes("Старый вопрос")));
});

// ============================================================================================
// Email sign-in
// ============================================================================================

scenario("the_email_form_sends_the_address_as_typed_and_shows_the_neutral_answer", async () => {
  const gate = deferred();
  const page = await open((env) => {
    env.net.route("GET", "/auth/me", notAuthenticated());
    env.net.route("POST", "/auth/email/request", async () => {
      await gate.promise;
      return json(202, { detail: "link_requested", message: "Если этот адрес есть в списке разрешенных, мы отправили письмо." });
    });
  });

  page.type("  Cook@Example.COM ", "email-input");
  page.el("email-form").fire("submit");
  page.el("email-form").fire("submit"); // twice
  await page.settle();
  assert.equal(page.el("email-submit").getAttribute("aria-disabled"), "true");
  assert.equal(page.el("email-result").textContent, "Отправляем…");
  gate.resolve();
  await page.settle();

  const requests = page.env.net.requests("POST", "/auth/email/request");
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body, { email: "Cook@Example.COM" });
  assert.equal(page.el("email-result").textContent, "Если этот адрес есть в списке разрешенных, мы отправили письмо.");
  assert.ok(page.el("email-result").className.includes("notice-ok"));
  assert.equal(page.el("email-submit").getAttribute("aria-disabled"), "false");
  assert.equal(page.el("email-submit").textContent, "Отправить ссылку еще раз");
});

scenario("a_bad_address_is_refused_on_the_page_and_a_failure_is_plain", async () => {
  const page = await open((env) => {
    env.net.route("GET", "/auth/me", notAuthenticated());
    env.net.route("POST", "/auth/email/request", json(503, { detail: "login_unavailable" }));
  });

  page.type("not an address", "email-input");
  page.el("email-form").fire("submit");
  await page.settle();
  assert.equal(page.el("email-result").textContent, Logic.TEXT.invalidEmail);
  assert.ok(page.el("email-result").className.includes("notice-error"));
  assert.equal(page.doc.activeElement, page.el("email-input"));
  assert.equal(page.env.net.requests("POST", "/auth/email/request").length, 0);

  page.type("cook@example.com", "email-input");
  page.el("email-form").fire("submit");
  await page.settle();
  assert.equal(page.el("email-result").textContent, Logic.TEXT.unavailable);
  assert.ok(!/hh-/.test(page.el("email-result").textContent));
});

// ============================================================================================
// Everything the script does to the page
// ============================================================================================

scenario("the_script_never_sets_a_handler_a_style_a_link_or_an_address_on_anything", async () => {
  const page = await open(oneConversation());
  page.env.net.route("POST", URL_A, fail(), apiError(502, "provider_error", { retryable: true }), turnBody(CONV_A, 3, HOSTILE, 4, HOSTILE));
  await page.send(HOSTILE);
  await page.settle();
  page.messages().byClass("msg-pending")[0].buttons("Повторить")[0].click();
  await page.settle();
  page.messages().byClass("msg-pending")[0].buttons("Повторить")[0].click();
  await page.settle();
  page.el("new-conversation").click();
  await page.settle();

  assert.ok(page.doc.attributeLog.length > 20, "the log saw the script's work");
  const names = new Set(page.doc.attributeLog.map(([, name]) => name));
  for (const name of names) {
    assert.ok(!/^on/i.test(name) && !["style", "href", "src", "srcdoc", "action", "formaction", "target"].includes(name), name);
  }
  assert.deepEqual([...names].sort().filter((n) => !["aria-busy", "aria-current", "aria-describedby", "aria-disabled", "aria-hidden", "aria-invalid", "aria-expanded", "aria-label", "aria-live", "aria-pressed", "autocapitalize", "autocomplete", "autocorrect", "data-drawer", "data-focus", "data-id", "datetime", "for", "id", "name", "novalidate", "role", "spellcheck", "tabindex", "type", "value"].includes(n)), []);
  assert.deepEqual(page.doc.root.byTag("script").length, 2);
});

scenario("the_view_has_everything_the_page_reads", async () => {
  const page = await open(oneConversation([message(3, "user", "Не вышло", { status: "failed", idempotency_key: "web-k3", error_code: "provider_error" })]));
  page.env.net.route("POST", URL_A, apiError(502, "provider_error", { retryable: true }));
  const probe = H.makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "а"), message(2, "assistant", "б"), message(3, "user", "в", { status: "in_progress", idempotency_key: "web-k" })]),
  });
  await probe.boot();
  const view = probe.view();

  // The names the script reads from a value called `name`: not from `els.name`, nor from `lastName`.
  const used = (name) =>
    new Set([...APP_SOURCE.matchAll(new RegExp("(?<![\\w.])" + name + "\\.(\\w+)", "g"))].map((m) => m[1]));
  assert.ok(used("view").size >= 8);
  for (const name of used("view")) assert.ok(name in view, "view." + name);
  for (const name of used("thread")) assert.ok(name in view.thread, "thread." + name);
  for (const name of used("composer")) assert.ok(name in view.composer, "composer." + name);
  for (const name of used("list")) assert.ok(name in view.list, "list." + name);

  // The favorites: the list beside the chat, the recipe in front, the question "remove it?", the mark under an answer.
  const keptEnv = H.makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "а"), message(2, "assistant", "б")]),
  });
  keptEnv.net.route("GET", "/api/favorites", H.favoritesBody([KEPT]));
  keptEnv.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(KEPT, FAV_RECIPE));
  await keptEnv.boot();
  await keptEnv.ctl.openFavorite(H.FAV_1);
  keptEnv.ctl.askRemoveFavorite(H.FAV_1);
  const kept = keptEnv.view();
  for (const name of used("favorites")) assert.ok(name in kept.favorites, "favorites." + name);
  for (const name of used("current")) assert.ok(name in kept.favorites.current, "current." + name);
  for (const name of used("removing")) assert.ok(name in kept.favorites.current.removing, "removing." + name);
  const markKeys = Object.keys(kept.thread.items.find((i) => i.role === "assistant").favorite);
  for (const name of used("mark")) assert.ok(markKeys.includes(name), "mark." + name);
  const favoriteKeys = Object.keys(kept.favorites.items[0]);
  for (const name of ["id", "title", "meta", "current"]) assert.ok(favoriteKeys.includes(name), "favorite item has " + name);
  for (const name of used("banner")) assert.ok(["kind", "text", "correlationId"].includes(name), "banner." + name);
  for (const name of used("notice")) assert.ok(["kind", "text"].includes(name), "notice." + name);

  // What the timeline's items carry, for every kind of item.
  probe.net.route("POST", URL_A, apiError(502, "provider_error", { retryable: true }));
  const itemKeys = new Set();
  const collect = (items) => items.forEach((item) => Object.keys(item).forEach((key) => itemKeys.add(key)));
  collect(view.thread.items);
  const failedEnv = H.makeEnv().signedIn([conversation(CONV_A, 0)]);
  failedEnv.net.route("POST", URL_A, apiError(502, "provider_error", { retryable: true }));
  await failedEnv.boot();
  await (await failedEnv.ctl.send("в")).done;
  collect(failedEnv.view().thread.items);
  const listKeys = new Set(Object.keys(view.list.items[0]));
  const favoriteItemKeys = new Set(Object.keys(kept.favorites.items[0]));
  for (const name of used("item")) {
    assert.ok(itemKeys.has(name) || listKeys.has(name) || favoriteItemKeys.has(name), "item." + name);
  }
  for (const name of ["id", "label", "meta", "current"]) assert.ok(listKeys.has(name), name);
  for (const name of ["type", "id", "role", "text", "time", "iso", "status", "note", "canRetry", "phase", "correlationId", "actions", "busy"]) {
    assert.ok(itemKeys.has(name), "no item carries " + name);
  }

  // The controller methods and the helpers the page calls exist.
  const controllerMethods = used("controller");
  assert.ok(controllerMethods.size >= 12);
  for (const name of controllerMethods) assert.equal(typeof probe.ctl[name], "function", "controller." + name);
  for (const name of used("Logic")) assert.ok(name in Logic, "Logic." + name);
});

// ============================================================================================
// Favorites
// ============================================================================================

const FAV_RECIPE = "Борщ классический\n\nПорции: 4.\n1. Сварите бульон.";
const KEPT = H.favorite(H.FAV_1, 2, "Борщ классический", 0);
const OTHER_KEPT = H.favorite(H.FAV_2, 12, "Плов с курицей", 30);

// One conversation (an answer with id 2), and these favorites on the server.
const withFavorites = (items = [KEPT, OTHER_KEPT], recipes = {}) => (env) => {
  oneConversation()(env);
  env.net.route("GET", "/api/favorites", H.favoritesBody(items));
  env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(KEPT, recipes[H.FAV_1] || FAV_RECIPE));
  env.net.route("GET", `/api/favorites/${H.FAV_2}`, H.favoriteDetail(OTHER_KEPT, recipes[H.FAV_2] || "Плов: рис, курица."));
};

scenario("the_tabs_swap_the_conversations_for_the_favorites_and_back", async () => {
  const page = await open(withFavorites());
  const state = () => ["panel-chats", "panel-favorites", "chat", "recipe"].map(page.visible);
  const pressed = () => [page.el("tab-chats").getAttribute("aria-pressed"), page.el("tab-favorites").getAttribute("aria-pressed")];

  assert.deepEqual(state(), [true, false, true, false]);
  assert.deepEqual(pressed(), ["true", "false"]);

  page.el("tab-favorites").click();
  await page.settle();
  assert.deepEqual(state(), [false, true, false, true]);
  assert.deepEqual(pressed(), ["false", "true"]);
  assert.deepEqual(
    page.el("favorite-list").byTag("button").map((b) => b.byClass("conv-label")[0].textContent),
    ["Борщ классический", "Плов с курицей"]
  );
  assert.equal(page.el("recipe-title").textContent, "Избранное");
  assert.ok(page.el("recipe-body").textContent.includes(Logic.TEXT.favoritesPick));

  page.el("tab-chats").click();
  await page.settle();
  assert.deepEqual(state(), [true, false, true, false]);
  assert.deepEqual(pressed(), ["true", "false"]);
});

scenario("an_empty_list_of_favorites_says_what_to_do", async () => {
  const page = await open(withFavorites([]));

  page.el("tab-favorites").click();
  await page.settle();

  assert.equal(page.el("favorites-status").textContent, Logic.TEXT.favoritesEmpty);
  assert.equal(page.el("favorite-list").childNodes.length, 0);
  assert.ok(page.el("recipe-body").textContent.includes(Logic.TEXT.favoritesEmpty));
});

scenario("with_structured_recipes_the_empty_favorites_hint_names_the_per_recipe_save", async () => {
  const page = await open(withFavorites([]), { structured: true });

  page.el("tab-favorites").click();
  await page.settle();

  assert.equal(page.el("favorites-status").textContent, Logic.TEXT.favoritesEmptyStructured);
  assert.ok(page.el("recipe-body").textContent.includes(Logic.TEXT.favoritesEmptyStructured));
  assert.ok(!page.el("recipe-body").textContent.includes(Logic.TEXT.favoritesEmpty));
});

scenario("a_favorite_that_cannot_be_listed_offers_to_try_again", async () => {
  const page = await open((env) => {
    withFavorites([KEPT])(env);
    env.net.route("GET", "/api/favorites", fail(), fail(), H.favoritesBody([KEPT])); // (the start, the tab, the button)
  });

  page.el("tab-favorites").click();
  await page.settle();
  assert.equal(page.el("favorites-status").textContent, Logic.TEXT.network);
  assert.equal(page.visible("favorites-retry"), true);

  page.el("favorites-retry").click();
  await page.settle();
  assert.equal(page.visible("favorites-retry"), false);
  assert.equal(page.el("favorite-list").byTag("button").length, 1);
});

scenario("an_answer_is_kept_from_the_chat_and_then_says_so_and_opens_the_recipe", async () => {
  const page = await open(withFavorites([]));
  page.env.net.route("POST", "/api/favorites", H.keptBody(KEPT));
  const article = () => page.articles()[1];
  assert.deepEqual(article().buttons("В избранное").length, 1);
  assert.equal(page.articles()[0].buttons("В избранное").length, 0, "only the assistant's answers");
  assert.ok(!article().textContent.includes("Сохранить «"), "no per-recipe controls without the switch");

  article().buttons("В избранное")[0].click();
  await page.settle();

  assert.equal(article().buttons("В избранное").length, 0);
  assert.ok(article().textContent.includes(Logic.TEXT.favoriteKept));
  assert.deepEqual(page.env.net.requests("POST", "/api/favorites")[0].body, { conversation_id: CONV_A, message_id: 2 });

  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(KEPT, FAV_RECIPE));
  article().buttons("Открыть")[0].click();
  await page.settle();
  assert.deepEqual(["chat", "recipe"].map(page.visible), [false, true]);
  assert.equal(page.el("recipe-title").textContent, "Борщ классический");
  assert.ok(page.el("recipe-body").textContent.includes("1. Сварите бульон."));
  assert.ok(page.doc.activeElement === page.el("recipe-title"));
});

scenario("a_failed_keep_says_so_under_the_answer_and_the_button_is_still_there", async () => {
  const page = await open(withFavorites([]));
  page.env.net.route("POST", "/api/favorites", apiError(409, "favorites_full"), H.keptBody(KEPT));
  const article = () => page.articles()[1];

  article().buttons("В избранное")[0].click();
  await page.settle();

  assert.ok(article().textContent.includes(Logic.CODE_MESSAGES.favorites_full));
  assert.equal(article().buttons("В избранное").length, 1);
  article().buttons("В избранное")[0].click();
  await page.settle();
  assert.ok(article().textContent.includes(Logic.TEXT.favoriteKept));
  assert.ok(!article().textContent.includes(Logic.CODE_MESSAGES.favorites_full));
});

scenario("the_text_of_a_recipe_stays_text_whatever_it_contains", async () => {
  const page = await open(withFavorites([KEPT], { [H.FAV_1]: HOSTILE }));

  page.el("tab-favorites").click();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  const body = page.el("recipe-body");
  assert.ok(body.textContent.includes("<script>alert(2)</script>"));
  assert.deepEqual(["img", "script", "svg", "a", "iframe"].map((tag) => body.byTag(tag).length), [0, 0, 0, 0, 0]);
  assert.equal(page.doc.root.byTag("script").length, 2, "only the page's own two scripts");
});

scenario("a_recipe_is_chosen_from_the_list_and_on_a_narrow_screen_the_drawer_closes", async () => {
  const page = await open(withFavorites(), { narrow: true });
  page.el("drawer-toggle").click();
  page.el("tab-favorites").click();
  await page.settle();
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "open");

  page.el("favorite-list").byTag("button")[1].click();
  await page.settle();

  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "closed");
  assert.equal(page.el("recipe-title").textContent, "Плов с курицей");
  assert.ok(page.doc.activeElement === page.el("recipe-title"));
  assert.equal(page.el("favorite-list").byTag("button")[1].getAttribute("aria-current"), "true");
});

scenario("a_recipe_that_fails_to_open_offers_to_try_again", async () => {
  const page = await open((env) => {
    withFavorites([KEPT])(env);
    env.net.route("GET", `/api/favorites/${H.FAV_1}`, fail(), H.favoriteDetail(KEPT, FAV_RECIPE));
  });

  page.el("tab-favorites").click();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  assert.ok(page.el("recipe-body").textContent.includes(Logic.TEXT.network));

  page.el("recipe-body").buttons("Повторить")[0].click();
  await page.settle();
  assert.ok(page.el("recipe-body").textContent.includes("1. Сварите бульон."));
});

scenario("removing_a_recipe_asks_first_and_the_safe_answer_is_the_one_in_focus", async () => {
  const page = await open(withFavorites());
  page.env.net.route("DELETE", `/api/favorites/${H.FAV_1}`, H.removedBody());
  page.el("tab-favorites").click();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  const body = page.el("recipe-body");

  body.buttons("Убрать из избранного")[0].click();
  await page.settle();
  assert.ok(body.textContent.includes(Logic.TEXT.removeAsk));
  assert.deepEqual(body.byClass("confirm")[0].byTag("button").map((b) => b.textContent), ["Убрать", "Оставить"]);
  assert.equal(page.doc.activeElement.textContent, "Оставить");
  assert.equal(page.env.net.requests("DELETE", /.*/).length, 0, "asking removes nothing");

  body.buttons("Оставить")[0].click();
  await page.settle();
  assert.ok(!body.textContent.includes(Logic.TEXT.removeAsk));
  assert.equal(page.doc.activeElement.textContent, "Убрать из избранного");
  assert.equal(page.env.net.requests("DELETE", /.*/).length, 0);

  body.buttons("Убрать из избранного")[0].click();
  await page.settle();
  body.buttons("Убрать")[0].click();
  await page.settle();

  assert.equal(page.env.net.requests("DELETE", /.*/).length, 1);
  assert.deepEqual(page.el("favorite-list").byTag("button").map((b) => b.textContent.includes("Плов")), [true]);
  assert.ok(page.el("recipe-body").textContent.includes(Logic.TEXT.favoritesPick));
  await page.env.timers.advance(100); // (a spoken line is set a moment after it is asked for)
  assert.equal(page.el("announcer").textContent, Logic.TEXT.removed);
});

scenario("a_removal_that_fails_says_so_and_can_be_repeated_or_called_off", async () => {
  const page = await open(withFavorites([KEPT]));
  page.env.net.route("DELETE", `/api/favorites/${H.FAV_1}`, apiError(503, "service_unavailable", { retryable: true }), H.removedBody());
  page.el("tab-favorites").click();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  const body = page.el("recipe-body");

  body.buttons("Убрать из избранного")[0].click();
  await page.settle();
  body.buttons("Убрать")[0].click();
  await page.settle();
  assert.ok(body.textContent.includes(Logic.CODE_MESSAGES.service_unavailable));
  assert.equal(page.el("favorite-list").byTag("button").length, 1, "still there");

  body.buttons("Повторить")[0].click();
  await page.settle();
  assert.equal(page.el("favorite-list").byTag("button").length, 0);
});

scenario("nothing_of_the_favorites_stays_on_the_page_after_signing_out", async () => {
  const page = await open(withFavorites());
  page.el("tab-favorites").click();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  assert.ok(page.root().includes("Сварите бульон"));

  page.env.net.route("POST", "/auth/logout", text(204, null));
  page.el("logout").click();
  await page.settle();

  assert.equal(page.visible("screen-login"), true);
  for (const words of ["Сварите бульон", "Борщ классический", "Плов с курицей", Logic.TEXT.favoritesEmpty]) {
    assert.ok(!page.root().includes(words), words);
  }
  assert.equal(page.el("favorite-list").childNodes.length, 0);
  assert.equal(page.el("recipe-body").childNodes.length, 0);
  assert.equal(page.el("recipe-title").textContent, "");
  assert.equal(page.visible("recipe"), false);
});

scenario("while_the_page_asks_who_is_signed_in_no_recipe_is_in_it_and_the_same_person_gets_it_back", async () => {
  const page = await open(withFavorites());
  page.el("tab-favorites").click();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  const wait = paused();
  page.env.net.route("GET", "/auth/me", wait.answer(me()));

  await comesBack(page);
  await flush();
  assert.equal(page.visible("screen-app"), false);
  for (const words of ["Сварите бульон", "Борщ классический", "Плов с курицей"]) {
    assert.ok(!page.root().includes(words), "in the page while asking: " + words);
  }
  wait.gate.resolve();
  await page.settle();

  assert.equal(page.visible("screen-app"), true);
  assert.deepEqual(["chat", "recipe"].map(page.visible), [false, true]);
  assert.ok(page.el("recipe-body").textContent.includes("Сварите бульон"));
});

scenario("a_different_person_on_coming_back_never_sees_the_last_ones_recipes", async () => {
  const page = await open(withFavorites());
  page.el("tab-favorites").click();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  page.env.net.route("GET", "/auth/me", me(86400, H.USER_B));
  page.env.net.route("GET", "/api/favorites", H.favoritesBody([]));

  await comesBack(page);
  await page.settle();

  assert.equal(page.visible("screen-app"), true);
  assert.deepEqual(["panel-chats", "chat", "recipe"].map(page.visible), [true, true, false]);
  assert.ok(!page.root().includes("Борщ классический"));
  assert.ok(!page.root().includes("Сварите бульон"));
});

// ============================================================================================
// The Telegram panel in the sidebar
// ============================================================================================

const TG_BOT = "house_helper_app_bot";
const TG_CODE = "k3JmQ0v9aZxP4tR7uYw2LbN6cD8eFgHs";
const TG_LINK = `https://t.me/${TG_BOT}?start=${TG_CODE}`;
const tgStatus = (linked, name = "@mama") =>
  json(200, linked
    ? { enabled: true, linked: true, bot_username: TG_BOT, account: { name, linked_at: "2026-10-18T12:00:00+00:00" } }
    : { enabled: true, linked: false, bot_username: TG_BOT });

const withTelegram = (routes) => (env) => {
  oneConversation()(env);
  for (const [key, ...responders] of routes) {
    const [method, route] = key.split(" ");
    env.net.route(method, route, ...responders);
  }
};

scenario("telegram_panel_is_not_drawn_when_the_bot_is_not_here_and_names_no_address", async () => {
  const page = await open(oneConversation());

  assert.equal(page.visible("telegram"), false);
  assert.equal(page.el("telegram-open").getAttribute("href"), null, "the shell names no address of its own");
  assert.ok(page.window.window === page.window);
});

scenario("telegram_panel_connects_draws_the_link_and_notices_the_pairing", async () => {
  const page = await open(withTelegram([
    ["GET /api/telegram", tgStatus(false), tgStatus(false), tgStatus(true)],
    ["POST /api/telegram/link", json(201, { code: TG_CODE, link: TG_LINK, expires_at: "x", expires_in_seconds: 600 })],
  ]));

  assert.equal(page.visible("telegram"), true);
  assert.equal(page.visible("telegram-connect"), true);
  assert.equal(page.visible("telegram-open"), false);
  assert.equal(page.visible("telegram-disconnect"), false);

  page.el("telegram-connect").click();
  await page.settle();
  assert.equal(page.visible("telegram-open"), true);
  assert.equal(page.el("telegram-open").getAttribute("href"), TG_LINK);
  assert.equal(page.el("telegram-open").getAttribute("target"), "_blank");
  assert.equal(page.el("telegram-open").getAttribute("rel"), "noopener noreferrer");
  assert.equal(page.visible("telegram-connect"), false);
  assert.ok(page.el("telegram-text").textContent.includes(`/start ${TG_CODE}`));

  // The only href this script ever writes is the validated link to the bot, on one anchor.
  const hrefs = page.doc.attributeLog.filter(([, name]) => name === "href");
  assert.deepEqual(hrefs, [["a", "href", TG_LINK]]);

  await page.env.timers.advance(3000);
  await page.settle();
  await page.env.timers.advance(3000);
  await page.settle();
  assert.equal(page.visible("telegram-open"), false);
  assert.equal(page.visible("telegram-disconnect"), true);
  assert.equal(page.el("telegram-text").textContent, "Подключен: @mama");
  assert.equal(page.el("telegram-notice").textContent, Logic.TELEGRAM_TEXT.pairedNow);
  assert.ok(!page.root().includes(TG_CODE), "the code is gone from the page once it is used");
});

scenario("telegram_panel_explains_the_copy_and_the_data_and_says_when_the_bot_is_down", async () => {
  const page = await open(withTelegram([["GET /api/telegram", tgStatus(false), json(200, { enabled: true, available: false, linked: false, bot_username: TG_BOT })]]));

  assert.equal(page.visible("telegram-privacy"), true);
  const privacy = page.el("telegram-privacy").textContent;
  assert.equal(privacy, Logic.TELEGRAM_TEXT.privacy);
  assert.ok(privacy.includes("OpenAI") && privacy.includes("365 дней") && privacy.includes("дублируется"));
  assert.equal(page.visible("telegram-notice"), false);
  assert.equal(page.visible("telegram-connect"), true);

  page.doc.visibilityState = "visible";
  page.doc.fire("visibilitychange");
  await page.settle();
  assert.equal(page.el("telegram-notice").textContent, Logic.TELEGRAM_TEXT.unavailable);
  assert.equal(page.visible("telegram-notice"), true);
  assert.equal(page.visible("telegram-connect"), false, "nothing to press while the bot is down");
});

scenario("telegram_panel_keeps_the_long_explanation_in_a_details_that_is_there_only_with_something_to_say", async () => {
  const gate = deferred();
  const page = await open(withTelegram([["GET /api/telegram", async () => {
    await gate.promise;
    return tgStatus(true);
  }]]));

  // While the state is being read there is nothing to explain yet: no empty expander.
  assert.equal(page.visible("telegram"), true);
  assert.equal(page.visible("telegram-more"), false);

  gate.resolve();
  await page.settle();
  const more = page.el("telegram-more");
  assert.equal(page.visible("telegram-more"), true);
  assert.equal(page.el("telegram-privacy").closest("details"), more, "the explanation is inside it");
  assert.equal(page.el("telegram-privacy").textContent, Logic.TELEGRAM_TEXT.privacy, "the whole text, not a shortened one");
  assert.ok(more.textContent.includes("Что происходит с данными"), "the summary says what is inside");
  // What the person needs at a glance is outside it.
  assert.equal(page.el("telegram-text").closest("details"), null);
  assert.equal(page.el("telegram-disconnect").closest("details"), null);
  assert.equal(page.el("telegram-text").textContent, Logic.TELEGRAM_TEXT.linkedAs.replace("{name}", "@mama"));
});

scenario("telegram_panel_explanation_is_open_while_the_account_is_not_linked_and_closed_once_it_is", async () => {
  const page = await open(withTelegram([
    ["GET /api/telegram", tgStatus(false), tgStatus(false), tgStatus(true)],
    ["POST /api/telegram/link", json(201, { code: TG_CODE, link: TG_LINK, expires_at: "x", expires_in_seconds: 600 })],
    ["DELETE /api/telegram/link", json(200, { linked: false })],
  ]));
  const more = page.el("telegram-more");

  // Not linked: the whole text is on the page without a tap, with the button that connects.
  assert.equal(page.visible("telegram-more"), true);
  assert.equal(more.getAttribute("open"), "", "open while the account is not linked");
  assert.equal(page.visible("telegram-connect"), true);
  assert.equal(page.el("telegram-privacy").textContent, Logic.TELEGRAM_TEXT.privacy);

  // Waiting for the person to press Start in Telegram is still "not linked".
  page.el("telegram-connect").click();
  await page.settle();
  assert.equal(page.visible("telegram-open"), true);
  assert.equal(more.open, true);
  await page.env.timers.advance(3000);
  await page.settle();
  assert.equal(more.open, true);

  // Linked: closed by default, but still there with its title and the whole text.
  await page.env.timers.advance(3000);
  await page.settle();
  assert.equal(page.visible("telegram-disconnect"), true);
  assert.equal(more.open, false);
  assert.equal(page.visible("telegram-more"), true, "reachable at all times");
  assert.equal(page.el("telegram-privacy").textContent, Logic.TELEGRAM_TEXT.privacy);

  // Asking about disconnecting does not change it; being unlinked again opens it again.
  page.el("telegram-disconnect").click();
  await page.settle();
  assert.equal(more.open, false);
  page.el("telegram-confirm").click();
  await page.settle();
  assert.equal(page.visible("telegram-connect"), true);
  assert.equal(more.open, true);
});

scenario("telegram_panel_leaves_the_readers_own_choice_of_the_explanation_alone", async () => {
  const page = await open(withTelegram([
    ["GET /api/telegram", tgStatus(false), tgStatus(false), tgStatus(true)],
    ["POST /api/telegram/link", json(201, { code: TG_CODE, link: TG_LINK, expires_at: "x", expires_in_seconds: 600 })],
  ]));
  const more = page.el("telegram-more");

  // Not linked: the reader closes it; the panel changing (the code, the polls) does not open it again.
  assert.equal(more.open, true);
  more.open = false;
  page.el("telegram-connect").click();
  await page.settle();
  assert.equal(page.visible("telegram-open"), true);
  assert.equal(more.open, false);
  await page.env.timers.advance(3000);
  await page.settle();
  assert.equal(more.open, false);

  // Linked: closed by default; the reader opens it; asking and cancelling leave it open.
  await page.env.timers.advance(3000);
  await page.settle();
  assert.equal(page.visible("telegram-disconnect"), true);
  assert.equal(more.open, false);
  more.open = true;
  page.el("telegram-disconnect").click();
  await page.settle();
  assert.equal(page.visible("telegram-confirm"), true);
  assert.equal(more.open, true);
  page.el("telegram-cancel").click();
  await page.settle();
  assert.equal(page.visible("telegram-disconnect"), true);
  assert.equal(more.open, true);
});

scenario("telegram_panel_explanation_is_open_when_the_state_could_not_be_read_and_closes_when_linked", async () => {
  const page = await open(withTelegram([["GET /api/telegram", fail(), tgStatus(true)]]));
  const more = page.el("telegram-more");

  // The state is not known: nothing says the account is linked, so the explanation is there.
  assert.equal(page.el("telegram-connect").textContent, Logic.TEXT.retry);
  assert.equal(page.visible("telegram-more"), true);
  assert.equal(more.open, true);

  page.el("telegram-connect").click();
  await page.settle();
  assert.equal(page.visible("telegram-disconnect"), true);
  assert.equal(more.open, false);
});

scenario("telegram_panel_explanation_is_open_again_for_the_next_person_who_is_not_linked", async () => {
  const page = await open(withTelegram([["GET /api/telegram", tgStatus(false), tgStatus(false)]]));
  const more = page.el("telegram-more");
  assert.equal(more.open, true);

  // Another person comes to the same tab; what the first one did with the panel is not carried over.
  more.open = false;
  page.env.net.route("GET", "/auth/me", me(86400, H.USER_B));
  page.env.net.route("GET", "/api/favorites", H.favoritesBody([]));
  await comesBack(page);
  await page.settle();

  assert.equal(page.visible("telegram-more"), true);
  assert.equal(page.visible("telegram-connect"), true);
  assert.equal(more.open, true, "open for a person whose account is not linked, as it is for anyone");
});

scenario("telegram_panel_a_name_is_text_and_never_markup", async () => {
  const page = await open(withTelegram([["GET /api/telegram", tgStatus(true, HOSTILE)]]));

  assert.equal(page.el("telegram-text").textContent, `Подключен: ${HOSTILE}`);
  assert.equal(page.doc.root.byTag("img").length, 0);
  assert.equal(page.doc.root.byTag("script").length, 2);
  assert.deepEqual(page.doc.attributeLog.filter(([, name]) => name === "href"), []);
});

scenario("telegram_panel_unlinking_asks_before_it_does_anything", async () => {
  const page = await open(withTelegram([
    ["GET /api/telegram", tgStatus(true)],
    ["DELETE /api/telegram/link", json(200, { linked: false })],
  ]));

  page.el("telegram-disconnect").click();
  await page.settle();
  assert.equal(page.visible("telegram-confirm"), true);
  assert.equal(page.visible("telegram-cancel"), true);
  assert.equal(page.visible("telegram-disconnect"), false);
  assert.equal(page.el("telegram-text").textContent, Logic.TELEGRAM_TEXT.askUnlink);
  assert.deepEqual(page.env.net.requests("DELETE", "/api/telegram/link"), []);

  page.el("telegram-cancel").click();
  await page.settle();
  assert.equal(page.visible("telegram-disconnect"), true);

  page.el("telegram-disconnect").click();
  await page.settle();
  page.el("telegram-confirm").click();
  await page.settle();
  assert.equal(page.env.net.requests("DELETE", "/api/telegram/link").length, 1);
  assert.equal(page.visible("telegram-connect"), true);
  assert.equal(page.el("telegram-notice").textContent, Logic.TELEGRAM_TEXT.unlinkedNow);
});

scenario("telegram_panel_goes_with_the_session_and_nothing_of_it_stays_for_the_next_person", async () => {
  const page = await open(withTelegram([["GET /api/telegram", tgStatus(true, "@сестра")]]));
  assert.ok(page.root().includes("@сестра"));

  page.env.net.route("POST", "/auth/logout", text(204, null));
  page.el("logout").click();
  await page.settle();
  assert.equal(page.visible("telegram"), false);
  assert.equal(page.visible("telegram-more"), false);
  assert.equal(page.el("telegram-more").open, false);
  assert.ok(!page.root().includes("@сестра"), "the name does not stay in the page");
});

scenario("telegram_panel_a_failure_is_said_with_a_retry", async () => {
  const page = await open(withTelegram([["GET /api/telegram", fail(), tgStatus(false)]]));

  assert.equal(page.visible("telegram"), true);
  assert.equal(page.el("telegram-notice").textContent, Logic.TEXT.network);
  assert.equal(page.el("telegram-connect").textContent, Logic.TEXT.retry);
  page.el("telegram-connect").click();
  await page.settle();
  assert.equal(page.el("telegram-connect").textContent, Logic.TELEGRAM_TEXT.connect);
});

// ============================================================================================
// A photo in the thread
// ============================================================================================

const DOM_PHOTO = "/api/attachments/5d6c1f3e-0a9b-4e43-9c2f-7b1a8d3e4f50";

scenario("a_photo_is_drawn_from_this_sites_own_route_and_only_that", async () => {
  const photo = (url) => [{ id: "5d6c1f3e-0a9b-4e43-9c2f-7b1a8d3e4f50", content_type: "image/jpeg", size: 10, url }];
  const page = await open(oneConversation([
    message(3, "user", "Смотрите", { attachments: photo(DOM_PHOTO) }),
    message(4, "assistant", "Вижу"),
    message(5, "user", "Подмена", { attachments: photo("https://evil.example/x.png") }),
  ]));

  const images = page.messages().byTag("img");
  assert.equal(images.length, 1, "only the address of the server's own route is drawn");
  assert.equal(images[0].getAttribute("src"), DOM_PHOTO);
  assert.equal(images[0].getAttribute("alt"), "Фото к сообщению");
  assert.ok(page.articleTexts().some((t) => t.includes("Подмена")), "the text of the other message is there");

  // The only src this script ever writes is that address, on an image; no other address, on nothing else.
  const sources = page.doc.attributeLog.filter(([, name]) => name === "src");
  assert.deepEqual(sources, [["img", "src", DOM_PHOTO]]);
  assert.ok(!page.doc.attributeLog.some(([, name, value]) => /^(on|style)/i.test(name) || /javascript:|evil/.test(value)));
});

scenario("a_photo_that_loads_late_keeps_the_newest_message_in_view_unless_the_reader_has_scrolled_away", async () => {
  const photo = [{ id: "5d6c1f3e-0a9b-4e43-9c2f-7b1a8d3e4f50", content_type: "image/jpeg", size: 10, url: DOM_PHOTO }];
  const page = await open(oneConversation([message(3, "user", "Смотрите", { attachments: photo }), message(4, "assistant", "Вижу")]));
  const box = page.messages();
  let height = 400;
  Object.defineProperty(box, "scrollHeight", { get: () => height });
  box.clientHeight = 300;
  const image = box.byTag("img")[0];

  // Drawn at the end of the thread (400 - 100 - 300 = 0 below the view); the photograph then gets its height.
  box.scrollTop = 100;
  box.fire("scroll");
  height = 800;
  image.fire("load");
  assert.equal(box.scrollTop, 800, "the end of the thread is brought back into view");

  // The reader has gone up to look at something: a photograph that loads now does not take them away.
  box.scrollTop = 100;
  box.fire("scroll");
  height = 1000;
  image.fire("error");
  assert.equal(box.scrollTop, 100);
});

scenario("no_photo_means_no_image", async () => {
  const page = await open(oneConversation());

  assert.equal(page.messages().byTag("img").length, 0);
  assert.deepEqual(page.doc.attributeLog.filter(([, name]) => name === "src"), []);
});

// ============================================================================================
// Structured recipes: one Save per recipe, and the editable card
// ============================================================================================

const ANSWER_TEXT = "Вот два варианта.";
const BORSH = {
  title: "Борщ",
  servings: "4 порции",
  ingredients: [
    { name: "свекла", amount: "2 шт." },
    { name: "капуста", amount: "300 г" },
  ],
  steps: ["Сварите бульон.", "Добавьте овощи."],
  notes: "Больше укропа.",
  version: 3,
};
const UKHA = { title: "Уха", servings: null, ingredients: [{ name: "рыба", amount: "1 кг" }], steps: ["Сварите."] };

const structuredThread = () => [
  message(1, "user", "Что приготовить?"),
  message(2, "assistant", ANSWER_TEXT, {
    recipes: [
      { position: 0, title: BORSH.title },
      { position: 1, title: UKHA.title },
    ],
  }),
];

const structuredFavoriteItem = (extra = {}) =>
  H.structuredFavorite(H.FAV_1, 2, BORSH.title, 0, 0, {
    servings: BORSH.servings,
    ingredients: BORSH.ingredients,
    steps: BORSH.steps,
    notes: BORSH.notes,
    version: BORSH.version,
    ...extra,
  });

function structuredThreadOnly() {
  return (env) => {
    env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
  };
}

function structuredCard(extraRoutes = () => {}) {
  return (env) => {
    env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
    const item = structuredFavoriteItem();
    env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
    env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ\n\n1. Сварите бульон."));
    extraRoutes(env, item);
  };
}

scenario("structured_under_an_answer_each_recipe_has_its_own_save_button", async () => {
  const page = await open(structuredThreadOnly(), { structured: true });
  let article = page.articles().find((node) => node.textContent.includes(ANSWER_TEXT));

  assert.equal(article.buttons("Сохранить «Борщ»").length, 1);
  assert.equal(article.buttons("Сохранить «Уха»").length, 1);
  assert.equal(article.buttons("В избранное").length, 0, "no whole-answer button when recipes are offered");
  assert.ok(!page.root().includes("<<<RECIPE>>>"));

  page.env.net.route("POST", "/api/favorites", H.keptBody(structuredFavoriteItem()));
  article.buttons("Сохранить «Борщ»")[0].click();
  await page.settle();

  assert.deepEqual(page.env.net.requests("POST", "/api/favorites")[0].body, {
    conversation_id: CONV_A,
    message_id: 2,
    recipe_index: 0,
  });
  article = page.articles().find((node) => node.textContent.includes(ANSWER_TEXT));
  assert.equal(article.buttons("Сохранить «Борщ»").length, 0);
  assert.equal(article.buttons("Сохранить «Уха»").length, 1, "the other recipe keeps its own control");
  assert.ok(article.textContent.includes(Logic.TEXT.recipeSaved));
  assert.equal(article.buttons(Logic.TEXT.openRecipe).length, 1);
});

scenario("structured_an_ordinary_answer_has_no_save_control_at_all", async () => {
  const page = await open(oneConversation(), { structured: true });
  const article = page.articles()[1];

  assert.equal(article.buttons("В избранное").length, 0, "the legacy whole-answer save is not a fallback");
  assert.ok(!article.textContent.includes("Сохранить «"));
  assert.equal(article.byClass("msg-favorite").length, 0);
});

scenario("structured_a_fresh_ordinary_answer_has_no_save_control", async () => {
  const page = await open(oneConversation(), { structured: true });
  page.env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Еще?", 4, "Просто совет."));
  await page.send("Еще?");
  await page.settle();

  const article = page.articles().find((node) => node.textContent.includes("Просто совет."));
  assert.equal(article.buttons("В избранное").length, 0, "no fallback for a freshly generated answer");
  assert.ok(!article.textContent.includes("Сохранить «"));
});

scenario("structured_an_answer_with_one_recipe_has_one_save_button", async () => {
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], {
        [CONV_A]: historyBody(CONV_A, [
          message(1, "user", "Что приготовить?"),
          message(2, "assistant", ANSWER_TEXT, { recipes: [{ position: 0, title: UKHA.title }] }),
        ]),
      });
    },
    { structured: true }
  );
  const article = page.articles().find((node) => node.textContent.includes(ANSWER_TEXT));

  assert.equal(article.buttons("Сохранить «Уха»").length, 1);
  assert.equal(article.buttons("В избранное").length, 0, "one recipe leaves no room for the whole-answer save");
});

scenario("structured_a_fresh_answer_with_one_recipe_has_one_save_button", async () => {
  const page = await open(oneConversation(), { structured: true });
  page.env.net.route(
    "POST",
    URL_A,
    turnBody(CONV_A, 3, "Еще?", 4, "Вот рецепт.", false, [{ position: 0, title: "Уха" }])
  );
  await page.send("Еще?");
  await page.settle();

  const article = page.articles().find((node) => node.textContent.includes("Вот рецепт."));
  assert.equal(article.buttons("Сохранить «Уха»").length, 1);
  assert.equal(article.buttons("В избранное").length, 0);
});

scenario("structured_a_malformed_recipe_list_has_no_save_control", async () => {
  const page = await open(
    oneConversation([message(3, "assistant", "Ответ с мусором.", { recipes: [{ position: "0", title: "Борщ" }, null] })]),
    { structured: true }
  );
  const article = page.articles().find((node) => node.textContent.includes("Ответ с мусором."));

  assert.equal(article.buttons("В избранное").length, 0, "no fallback to the whole-answer save");
  assert.ok(!article.textContent.includes("Сохранить «"));
  assert.equal(article.byClass("msg-favorite").length, 0);
});

scenario("structured_favorites_remain_accessible_editable_and_removable_whatever_the_switch", async () => {
  for (const structured of [false, true]) {
    const page = await open(
      (env) => {
        env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
        const item = structuredFavoriteItem();
        env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
        env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ\n\n1. Сварите бульон."));
      },
      { structured }
    );
    page.el("tab-favorites").click();
    await page.settle();
    page.el("favorite-list").byTag("button")[0].click();
    await page.settle();

    const body = page.el("recipe-body");
    const label = "switch " + (structured ? "on" : "off");
    assert.ok(body.textContent.includes("4 порции"), label);
    assert.equal(body.buttons("Редактировать").length, 1, label);
    body.buttons("Редактировать")[0].click();
    await page.settle();
    assert.equal(page.el("recipe-body").byClass("edit-title")[0].value, "Борщ", label);

    page.el("recipe-body").buttons(Logic.TEXT.editCancel)[0].click();
    await page.settle();
    page.env.net.route("DELETE", `/api/favorites/${H.FAV_1}`, H.removedBody());
    page.el("recipe-body").buttons("Убрать из избранного")[0].click();
    await page.settle();
    page.el("recipe-body").buttons("Убрать")[0].click();
    await page.settle();
    assert.equal(page.env.net.requests("DELETE", `/api/favorites/${H.FAV_1}`).length, 1, label);
    assert.equal(page.el("favorite-list").byTag("button").length, 0, label);
  }
});

scenario("structured_the_card_shows_its_fields_and_is_edited_in_the_page", async () => {
  const page = await open(structuredCard(), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  const body = page.el("recipe-body");
  assert.ok(body.textContent.includes("4 порции"));
  assert.ok(body.textContent.includes("свекла") && body.textContent.includes("2 шт."));
  assert.ok(body.textContent.includes("Сварите бульон."));
  assert.ok(body.textContent.includes(BORSH.notes));
  assert.equal(body.buttons("Редактировать").length, 1);
  assert.equal(body.buttons("Убрать из избранного").length, 1);

  body.buttons("Редактировать")[0].click();
  await page.settle();
  assert.equal(page.el("recipe-body").byClass("edit-title")[0].value, "Борщ");
  assert.deepEqual(page.el("recipe-body").byClass("ingredient-name").map((input) => input.value), ["свекла", "капуста"]);
  assert.deepEqual(page.el("recipe-body").byClass("ingredient-amount").map((input) => input.value), ["2 шт.", "300 г"]);
  assert.deepEqual(page.el("recipe-body").byClass("edit-step").map((input) => input.value), BORSH.steps);

  page.el("recipe-body").byClass("edit-title")[0].value = "Борщ по-домашнему";
  page.el("recipe-body").buttons(Logic.TEXT.editAddStep)[0].click();
  await page.settle();
  assert.equal(page.el("recipe-body").byClass("edit-step").length, 3);
  page.el("recipe-body").byClass("edit-step")[2].value = "Дайте настояться.";
  page.el("recipe-body").byClass("edit-notes")[0].value = "Еще укропа.";
  // Reorder: the first step goes below the second.
  page.el("recipe-body").buttons(Logic.TEXT.editMoveDown)[0].click();
  await page.settle();
  assert.deepEqual(
    page.el("recipe-body").byClass("edit-step").map((input) => input.value),
    ["Добавьте овощи.", "Сварите бульон.", "Дайте настояться."]
  );

  const updated = structuredFavoriteItem({
    title: "Борщ по-домашнему",
    steps: ["Добавьте овощи.", "Сварите бульон.", "Дайте настояться."],
    notes: "Еще укропа.",
    version: 4,
  });
  page.env.net.route(
    "PATCH",
    `/api/favorites/${H.FAV_1}`,
    H.favoriteDetail(updated, "Борщ по-домашнему\n\n1. Сварите бульон.")
  );
  page.el("recipe-body").buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();

  const sent = page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`)[0].body;
  assert.deepEqual(sent, {
    expected_version: 3,
    title: "Борщ по-домашнему",
    notes: "Еще укропа.",
    servings: "4 порции",
    ingredients: BORSH.ingredients,
    steps: ["Добавьте овощи.", "Сварите бульон.", "Дайте настояться."],
  });
  assert.equal(page.el("recipe-title").textContent, "Борщ по-домашнему");
  assert.equal(page.el("recipe-body").buttons(Logic.TEXT.editSave).length, 0);
  assert.ok(page.el("recipe-body").textContent.includes("Дайте настояться."));
  assert.ok(
    !page.doc.attributeLog.some(([, name, value]) => /^(on|style)/i.test(name) || /javascript:/.test(value))
  );
});

scenario("structured_a_step_is_a_wrapping_textarea_and_line_breaks_become_spaces", async () => {
  const page = await open(structuredCard(), { structured: true });
  await openEditor(page);
  const body = () => page.el("recipe-body");
  const fields = body().byClass("edit-step");

  // A long instruction wraps in a textarea instead of being clipped to one line.
  assert.deepEqual(fields.map((field) => field.tagName), ["TEXTAREA", "TEXTAREA"]);
  assert.deepEqual(fields.map((field) => field.getAttribute("rows")), ["3", "3"]);
  // Enter is a step boundary, not a line break inside a step: the server's rule is one line.
  assert.equal(
    fields[0].fire("keydown", { key: "Enter" }).defaultPrevented,
    true,
    "Enter does not put a second line into a step"
  );
  // A pasted line break becomes the space it stands for; the rest of the text is untouched,
  // so the draft can still be saved under the existing validation.
  page.editField(fields[0], "Сварите\nбульон.\n\nДобавьте овощи.");
  assert.equal(body().byClass("edit-step")[0].value, "Сварите бульон.  Добавьте овощи.");
  assert.equal(body().byClass("edit-step")[1].value, BORSH.steps[1], "the other steps are untouched");
  assert.equal(page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`).length, 0);
});

scenario("structured_hostile_recipe_fields_are_only_text", async () => {
  const hostileTitle = '<img src=x onerror=alert(1)>';
  const hostileName = '<script>alert(2)</script>';
  const hostileAmount = '"><svg onload=alert(3)>';
  const hostileStep = "</div><button onclick=alert(4)>нажми</button>";
  const item = structuredFavoriteItem({
    title: hostileTitle,
    ingredients: [{ name: hostileName, amount: hostileAmount }],
    steps: [hostileStep],
    notes: "<b>заметка</b>",
  });
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
      env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "текст"));
    },
    { structured: true }
  );
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  const body = page.el("recipe-body");
  assert.equal(body.byTag("img").length, 0);
  assert.equal(body.byTag("script").length, 0);
  assert.equal(body.byTag("svg").length, 0);
  assert.equal(body.byTag("b").length, 0);
  assert.equal(page.el("recipe-title").textContent, hostileTitle);
  assert.ok(body.textContent.includes(hostileName));
  assert.ok(body.textContent.includes(hostileAmount));
  assert.ok(body.textContent.includes(hostileStep));
  assert.ok(body.textContent.includes("<b>заметка</b>"));

  body.buttons("Редактировать")[0].click();
  await page.settle();
  const edited = page.el("recipe-body");
  assert.equal(edited.byClass("edit-title")[0].value, hostileTitle);
  assert.equal(edited.byClass("ingredient-name")[0].value, hostileName);
  assert.equal(edited.byClass("edit-step")[0].value, hostileStep);
  assert.ok(!page.doc.attributeLog.some(([, name]) => /^(on|style)/i.test(name)));
});

scenario("structured_a_conflict_shows_the_question_and_the_two_choices", async () => {
  const page = await open(structuredCard(), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  page.el("recipe-body").buttons("Редактировать")[0].click();
  await page.settle();

  page.el("recipe-body").byClass("edit-title")[0].value = "Черновик";
  page.env.net.route("PATCH", `/api/favorites/${H.FAV_1}`, apiError(409, "favorite_conflict"));
  page.el("recipe-body").buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();

  const body = page.el("recipe-body");
  assert.ok(body.textContent.includes(Logic.TEXT.editConflict));
  assert.equal(body.buttons(Logic.TEXT.editOverwrite).length, 1);
  assert.equal(body.buttons(Logic.TEXT.editReload).length, 1);
  assert.equal(body.buttons(Logic.TEXT.editSave).length, 0);
  assert.equal(body.byClass("edit-title")[0].value, "Черновик", "the draft is still in the page");

  const changed = structuredFavoriteItem({ title: "Изменено в другом месте", version: 5 });
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(changed, "Борщ\n\n1. Сварите бульон."));
  body.buttons(Logic.TEXT.editReload)[0].click();
  await page.settle();

  const after = page.el("recipe-body");
  assert.equal(page.el("recipe-title").textContent, "Изменено в другом месте");
  assert.equal(after.buttons(Logic.TEXT.editSave).length, 0);
  assert.equal(after.byClass("edit-title").length, 0);
});

scenario("structured_an_unsaved_editor_is_not_left_without_a_question", async () => {
  const page = await open(structuredCard(), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  page.el("recipe-body").buttons("Редактировать")[0].click();
  await page.settle();

  let prevented = false;
  page.fireWindow("beforeunload", {
    preventDefault() {
      prevented = true;
    },
  });
  assert.equal(prevented, true, "the browser is asked to warn about a refresh or close");

  let asked = 0;
  page.window.confirm = () => {
    asked++;
    return false;
  };
  page.el("logout").click();
  await page.settle();
  assert.equal(asked, 1);
  assert.equal(page.visible("screen-app"), true, "a refused question leaves the person where they were");
  assert.equal(page.env.net.requests("POST", "/auth/logout").length, 0);

  page.el("recipe-body").buttons(Logic.TEXT.editCancel)[0].click();
  await page.settle();
  prevented = false;
  page.fireWindow("beforeunload", {
    preventDefault() {
      prevented = true;
    },
  });
  assert.equal(prevented, false);
});

// --- an unsaved edit survives the page's own refreshes --------------------------------------

const TEXT_RECIPE = "Борщ классический\n\nПорции: 4.\n1. Сварите бульон.";

function textCard() {
  return (env) => {
    env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
    const item = H.favorite(H.FAV_1, 2, "Борщ классический", 0, { version: 2 });
    env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
    env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, TEXT_RECIPE));
  };
}

async function openEditor(page) {
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  page.el("recipe-body").buttons(Logic.TEXT.editAction)[0].click();
  await page.settle();
}

scenario("structured_an_unsaved_edit_survives_a_tab_switch_and_an_online_event_whatever_the_switch", async () => {
  // The review's reproduction: edit a saved recipe, switch tabs, come back. The page
  // asks the server who is signed in on the way (and holds the app back while it does),
  // which used to redraw the editor from the fields as they were when editing began.
  for (const structured of [false, true]) {
    const page = await open(structuredCard(), { structured });
    const label = "switch " + (structured ? "on" : "off");
    await openEditor(page);

    const body = () => page.el("recipe-body");
    page.editField(body().byClass("edit-title")[0], "UNSAVED REVIEW DRAFT");
    page.editField(body().byClass("ingredient-name")[1], "морковь");
    page.editField(body().byClass("ingredient-amount")[1], "2 шт.");
    page.editField(body().byClass("edit-notes")[0], "Черновик заметки");
    body().buttons(Logic.TEXT.editAddStep)[0].click();
    await page.settle();
    page.editField(body().byClass("edit-step")[2], "Дайте настояться.");
    body().buttons(Logic.TEXT.editMoveDown)[0].click();
    await page.settle();
    page.editField(body().byClass("edit-title")[0], "UNSAVED REVIEW DRAFT");

    // Away and back: the session is checked, and the same person keeps the editor.
    page.doc.visibilityState = "hidden";
    page.doc.fire("visibilitychange");
    await page.settle();
    page.doc.visibilityState = "visible";
    page.doc.fire("visibilitychange");
    await page.settle();

    assert.equal(page.visible("screen-app"), true, label);
    const after = page.el("recipe-body");
    assert.equal(after.byClass("edit-title")[0].value, "UNSAVED REVIEW DRAFT", label);
    assert.deepEqual(after.byClass("ingredient-name").map((input) => input.value), ["свекла", "морковь"], label);
    assert.deepEqual(after.byClass("ingredient-amount").map((input) => input.value), ["2 шт.", "2 шт."], label);
    assert.deepEqual(
      after.byClass("edit-step").map((input) => input.value),
      ["Добавьте овощи.", "Сварите бульон.", "Дайте настояться."],
      label + ": every row and its order"
    );
    assert.equal(after.byClass("edit-notes")[0].value, "Черновик заметки", label);
    assert.equal(page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`).length, 0, label + ": nothing was saved");

    // The network comes back: the same editor and the same draft.
    page.editField(page.el("recipe-body").byClass("edit-title")[0], "STILL NOT SAVED");
    page.fireWindow("online");
    await page.settle();
    assert.equal(page.el("recipe-body").byClass("edit-title")[0].value, "STILL NOT SAVED", label);
    assert.equal(page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`).length, 0, label);

    // Saving is still an explicit act, and it sends exactly the draft on the page.
    const updated = structuredFavoriteItem({
      title: "STILL NOT SAVED",
      ingredients: [
        { name: "свекла", amount: "2 шт." },
        { name: "морковь", amount: "2 шт." },
      ],
      steps: ["Добавьте овощи.", "Сварите бульон.", "Дайте настояться."],
      notes: "Черновик заметки",
      version: 4,
    });
    page.env.net.route("PATCH", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(updated, "новый текст"));
    page.el("recipe-body").buttons(Logic.TEXT.editSave)[0].click();
    await page.settle();

    const sent = page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`)[0].body;
    assert.deepEqual(sent, {
      expected_version: 3,
      title: "STILL NOT SAVED",
      notes: "Черновик заметки",
      servings: "4 порции",
      ingredients: [
        { name: "свекла", amount: "2 шт." },
        { name: "морковь", amount: "2 шт." },
      ],
      steps: ["Добавьте овощи.", "Сварите бульон.", "Дайте настояться."],
    }, label);
    assert.equal(page.el("recipe-title").textContent, "STILL NOT SAVED", label);

    // A refresh with no editor open still works: the stored card stays as it was saved.
    page.doc.visibilityState = "visible";
    page.doc.fire("visibilitychange");
    await page.settle();
    assert.equal(page.el("recipe-body").buttons(Logic.TEXT.editSave).length, 0, label);
    assert.ok(page.el("recipe-body").textContent.includes("Черновик заметки"), label);
  }
});

scenario("a_legacy_text_draft_survives_a_tab_switch_and_an_online_event", async () => {
  const page = await open(textCard(), { structured: true });
  await openEditor(page);

  const body = () => page.el("recipe-body");
  assert.equal(body().byClass("edit-step").length, 0, "a text favorite has no structured rows");
  page.editField(body().byClass("edit-title")[0], "Мой борщ");
  page.editField(body().byClass("edit-text")[0], "Борщ по-моему.\n\n1. Сварить.");
  page.editField(body().byClass("edit-notes")[0], "Больше свеклы.");

  page.doc.visibilityState = "hidden";
  page.doc.fire("visibilitychange");
  await page.settle();
  page.doc.visibilityState = "visible";
  page.doc.fire("visibilitychange");
  await page.settle();

  const after = page.el("recipe-body");
  assert.equal(after.byClass("edit-title")[0].value, "Мой борщ");
  assert.equal(after.byClass("edit-text")[0].value, "Борщ по-моему.\n\n1. Сварить.");
  assert.equal(after.byClass("edit-notes")[0].value, "Больше свеклы.");
  assert.equal(page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`).length, 0);

  page.editField(page.el("recipe-body").byClass("edit-text")[0], "Совсем другой текст.");
  page.fireWindow("online");
  await page.settle();
  assert.equal(page.el("recipe-body").byClass("edit-text")[0].value, "Совсем другой текст.");
  assert.equal(page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`).length, 0);

  const saved = H.favorite(H.FAV_1, 2, "Мой борщ", 0, { version: 3 });
  page.env.net.route(
    "PATCH",
    `/api/favorites/${H.FAV_1}`,
    H.favoriteDetail(saved, "Совсем другой текст.", { notes: "Больше свеклы." })
  );
  page.el("recipe-body").buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();

  assert.deepEqual(page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`)[0].body, {
    expected_version: 2,
    title: "Мой борщ",
    text: "Совсем другой текст.",
    notes: "Больше свеклы.",
  });
  assert.equal(page.el("recipe-title").textContent, "Мой борщ");
});

// --- the editor keeps every character the person typed ---------------------------------------

scenario("structured_a_step_of_emoji_and_ascii_survives_an_online_event_and_is_saved_whole", async () => {
  // The review's reproduction: a valid step of 1,000 Unicode characters (500 outside the
  // basic plane), cut to 501 by the old UTF-16 .slice() on the next page refresh.
  const step = "🍅".repeat(500) + "a".repeat(500);
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      const item = structuredFavoriteItem({ steps: [step, "Добавьте овощи."] });
      env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
      env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ\n\n1. Сварите бульон."));
    },
    { structured: true }
  );
  await openEditor(page);
  const body = () => page.el("recipe-body");
  assert.equal(body().byClass("edit-step")[0].value, step);

  page.editField(body().byClass("edit-title")[0], "Борщ по-домашнему");

  // Away and back: the session is checked and the editor is redrawn from the draft.
  page.doc.visibilityState = "hidden";
  page.doc.fire("visibilitychange");
  await page.settle();
  page.doc.visibilityState = "visible";
  page.doc.fire("visibilitychange");
  await page.settle();
  assert.equal(page.el("recipe-body").byClass("edit-step")[0].value, step);

  // The network comes back: the same, and nothing is sent.
  page.fireWindow("online");
  await page.settle();
  let after = page.el("recipe-body");
  assert.equal(after.byClass("edit-step")[0].value, step);
  assert.equal(Logic.countCodePoints(after.byClass("edit-step")[0].value), 1000);
  assert.equal(page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`).length, 0, "a refresh saves nothing");

  // A re-render of the form (a row added) keeps the same characters.
  after.buttons(Logic.TEXT.editAddStep)[0].click();
  await page.settle();
  after = page.el("recipe-body");
  assert.equal(after.byClass("edit-step")[0].value, step);
  page.editField(after.byClass("edit-step")[2], "Дайте настояться.");

  const updated = structuredFavoriteItem({
    title: "Борщ по-домашнему",
    steps: [step, "Добавьте овощи.", "Дайте настояться."],
    version: 4,
  });
  page.env.net.route("PATCH", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(updated, "Борщ"));
  page.el("recipe-body").buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();

  const sent = page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`)[0].body;
  assert.deepEqual(sent.steps, [step, "Добавьте овощи.", "Дайте настояться."]);
  assert.equal(Logic.countCodePoints(sent.steps[0]), 1000);
  assert.equal(page.env.net.log.filter((request) => request.method === "POST").length, 0, "no model is called");
});

scenario("a_legacy_text_of_one_hundred_thousand_characters_survives_the_page_refreshes_and_saves_whole", async () => {
  const legacy = "🍅".repeat(50000) + "a".repeat(50000); // 100,000 code points, 150,000 UTF-16 units
  const page = await open((env) => {
    env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
    const item = H.favorite(H.FAV_1, 2, "Старый текст", 0, { version: 2 });
    env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
    env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, legacy));
  });
  await openEditor(page);
  const body = () => page.el("recipe-body");
  assert.equal(body().byClass("edit-text")[0].value, legacy);
  assert.equal(body().byClass("edit-text")[0].getAttribute("maxlength"), null, "no UTF-16 maxlength");

  page.editField(body().byClass("edit-title")[0], "Новое имя");
  page.doc.visibilityState = "hidden";
  page.doc.fire("visibilitychange");
  await page.settle();
  page.doc.visibilityState = "visible";
  page.doc.fire("visibilitychange");
  await page.settle();

  assert.equal(page.el("recipe-body").byClass("edit-text")[0].value, legacy);
  assert.equal(page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`).length, 0);

  const saved = H.favorite(H.FAV_1, 2, "Новое имя", 0, { version: 3 });
  page.env.net.route("PATCH", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(saved, legacy));
  page.el("recipe-body").buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();

  const sent = page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`)[0].body;
  assert.equal(sent.text, legacy);
  assert.equal(sent.title, "Новое имя");
});

scenario("an_over_limit_field_shows_an_explicit_reason_and_the_whole_draft_stays", async () => {
  const page = await open(structuredCard(), { structured: true });
  await openEditor(page);
  const body = () => page.el("recipe-body");
  const tooLong = "🍅".repeat(1001); // 1,001 code points: one over the server's step length

  page.editField(body().byClass("edit-step")[0], tooLong);
  page.editField(body().byClass("edit-title")[0], "Черновик");
  page.editField(body().byClass("edit-notes")[0], "Не потерять");
  body().buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();

  const refused = page.el("recipe-body");
  assert.ok(refused.textContent.includes(Logic.TEXT.editStepTooLong), "an explicit Russian reason is shown");
  assert.equal(refused.byClass("edit-step")[0].value, tooLong, "the whole overdraft stays in the field");
  assert.equal(refused.byClass("edit-title")[0].value, "Черновик");
  assert.equal(refused.byClass("edit-notes")[0].value, "Не потерять");
  assert.equal(
    page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`).length,
    0,
    "nothing over the limit is sent"
  );

  // Nothing was cut: shortening the field saves every other field exactly as it was left.
  page.editField(page.el("recipe-body").byClass("edit-step")[0], "Сварите бульон.");
  const updated = structuredFavoriteItem({ title: "Черновик", notes: "Не потерять", version: 4 });
  page.env.net.route("PATCH", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(updated, "Борщ"));
  page.el("recipe-body").buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();

  const sent = page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`)[0].body;
  assert.equal(sent.title, "Черновик");
  assert.equal(sent.notes, "Не потерять");
  assert.deepEqual(sent.steps, ["Сварите бульон.", "Добавьте овощи."]);
});

scenario("the_editor_fields_carry_no_utf16_maxlength_and_a_full_astral_title_saves", async () => {
  const page = await open(structuredCard(), { structured: true });
  await openEditor(page);
  const body = () => page.el("recipe-body");
  for (const selector of ["edit-title", "edit-servings", "ingredient-name", "ingredient-amount", "edit-step", "edit-notes"]) {
    for (const field of body().byClass(selector)) {
      assert.equal(field.getAttribute("maxlength"), null, selector + " must not carry a UTF-16 maxlength");
    }
  }

  // 200 code points outside the basic plane are exactly the server's title maximum, though
  // the browser's maxlength would count them as 400 and block the last 100.
  const title = "🍅".repeat(200);
  page.editField(body().byClass("edit-title")[0], title);
  const updated = structuredFavoriteItem({ title, version: 4 });
  page.env.net.route("PATCH", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(updated, "Борщ"));
  body().buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();

  const sent = page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`)[0].body;
  assert.equal(sent.title, title);
  assert.equal(Logic.countCodePoints(sent.title), 200);
});

scenario("a_draft_is_never_shown_after_the_session_ends_or_another_person_signs_in", async () => {
  // The session ends: nothing of the draft stays on the page.
  const ended = await open(structuredCard(), { structured: true });
  await openEditor(ended);
  ended.editField(ended.el("recipe-body").byClass("edit-title")[0], "UNSAVED REVIEW DRAFT");
  ended.env.net.route("GET", "/auth/me", notAuthenticated());
  ended.doc.visibilityState = "visible";
  ended.doc.fire("visibilitychange");
  await ended.settle();

  assert.equal(ended.visible("screen-login"), true);
  assert.ok(!ended.root().includes("UNSAVED REVIEW DRAFT"), "the draft is gone from the page");
  assert.equal(ended.el("recipe-body").textContent, "");

  // Another person signs in in the same browser: the page starts over, without the draft.
  const other = await open(structuredCard(), { structured: true });
  await openEditor(other);
  other.editField(other.el("recipe-body").byClass("edit-title")[0], "UNSAVED REVIEW DRAFT");
  other.env.net.route("GET", "/auth/me", me(86400, H.USER_B));
  other.env.net.route("GET", "/api/conversations", listBody([conversation(CONV_B, 0)]));
  other.env.net.route(
    "GET",
    new RegExp("/api/conversations/" + CONV_B + "/messages(\\?|$)"),
    historyBody(CONV_B, [message(7, "user", "Про суп Бориса"), message(8, "assistant", "Суп так")])
  );
  other.env.net.route("GET", "/api/favorites", H.favoritesBody([]));
  other.doc.visibilityState = "visible";
  other.doc.fire("visibilitychange");
  await other.settle();

  assert.equal(other.visible("screen-app"), true);
  assert.ok(!other.root().includes("UNSAVED REVIEW DRAFT"));
  assert.ok(!other.root().includes("Борщ"), "nothing of the last person's card is left");
  assert.equal(other.el("recipe-body").byClass("edit-title").length, 0);
});

// ============================================================================================
// Shopping lists
// ============================================================================================

const SHOP_URL = "/api/shopping-lists";
const SHOP_LIST_URL = `/api/shopping-lists/${H.LIST_1}`;
const SHOP_LIST_URL_DONE = `/api/shopping-lists/${H.LIST_2}`;
const CHAT_RECIPE_URL = `/api/conversations/${CONV_A}/messages/2/recipes/0`;
const SHOP_INGREDIENTS = [
  { name: "свёкла", amount: "2 шт." },
  { name: "мясо", amount: "500 г" },
];
const SHOP_ITEMS = [
  H.shoppingItem(H.ITEM_1, 0, "свёкла", "2 шт.", "at_home"),
  H.shoppingItem(H.ITEM_2, 1, "мясо", "500 г", "unchecked"),
  H.shoppingItem(H.ITEM_3, 2, "сметана", "200 г", "purchased"),
];
const SHOP_DETAIL = H.shoppingDetail(H.LIST_1, "Борщ", "active", SHOP_ITEMS);
const SHOP_DETAIL_DONE = H.shoppingDetail(
  H.LIST_2,
  "Плов",
  "completed",
  [H.shoppingItem(H.ITEM_3, 0, "рис", "200 г", "purchased")]
);

// A conversation whose last answer carries one structured recipe, and the review of that
// recipe's stored rows that the draft asks for.
const withStructuredRecipe = (title = "Борщ", ingredients = SHOP_INGREDIENTS) => (env) => {
  env.signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [
      message(1, "user", "Приготовь борщ"),
      message(2, "assistant", "Борщ: свекла, мясо.", { recipes: [{ position: 0, title }] }),
    ]),
  });
  env.net.route("GET", CHAT_RECIPE_URL, H.recipeBody(2, 0, title, "4 порции", ingredients, ["Сварите бульон."]));
};

const shoppingLists = (details) => H.shoppingListsBody(details.map((detail) => H.shoppingSummary(detail)));

scenario("every_structured_recipe_offers_what_to_buy_beside_its_save_control", async () => {
  const page = await open(withStructuredRecipe(), { structured: true });

  const article = page.articles()[1];
  assert.equal(article.buttons("Сохранить «Борщ»").length, 1);
  assert.equal(article.buttons("Что купить?").length, 1);
  assert.equal(page.articles()[0].buttons("Что купить?").length, 0, "only the assistant's structured recipes");

  article.buttons("Что купить?")[0].click();
  await page.settle();

  assert.deepEqual(["chat", "recipe", "shopping"].map(page.visible), [false, false, true]);
  assert.equal(page.el("tab-shopping").getAttribute("aria-pressed"), "true");
  assert.equal(page.el("shopping-title").textContent, "Что купить?");
  const body = page.el("shopping-body");
  assert.ok(body.textContent.includes("Борщ"));
  assert.ok(body.textContent.includes("4 порции"));
  assert.ok(body.textContent.includes(Logic.TEXT.shoppingDraftInstruction));
  assert.ok(body.textContent.includes("Проверить / купить: 2"));
  assert.ok(body.textContent.includes(Logic.TEXT.shoppingUncheckedNote));
  assert.deepEqual(body.byTag("input").map((input) => input.getAttribute("type")), ["checkbox", "checkbox"]);
  assert.deepEqual(body.byTag("input").map((input) => input.getAttribute("aria-label")), ["Есть дома: свёкла", "Есть дома: мясо"]);
  assert.equal(body.buttons("Сохранить список").length, 1);
  assert.equal(body.buttons("Отмена").length, 1);
  assert.equal(page.env.net.requests("POST", SHOP_URL).length, 0, "nothing is saved by opening the draft");
});

scenario("marking_ingredients_at_home_counts_them_and_saving_opens_the_saved_list", async () => {
  const page = await open(withStructuredRecipe(), { structured: true });
  page.env.net.route("POST", SHOP_URL, json(201, SHOP_DETAIL));
  page.articles()[1].buttons("Что купить?")[0].click();
  await page.settle();
  const body = () => page.el("shopping-body");

  const first = body().byTag("input")[0];
  first.checked = true;
  first.fire("change");
  await page.settle();
  assert.ok(body().textContent.includes("Проверить / купить: 1"));

  body().buttons("Сохранить список")[0].click();
  await page.settle();

  assert.deepEqual(page.env.net.requests("POST", SHOP_URL)[0].body.item_states, [{ position: 0, state: "at_home" }]);
  assert.equal(page.el("shopping-title").textContent, "Борщ");
  assert.ok(body().textContent.includes("Куплено 1 из 3"));
  assert.ok(body().textContent.includes("Проверить / купить: 1"));
  assert.ok(body().textContent.includes("Есть дома: 1"));
  assert.ok(body().textContent.includes("Куплено: 1"));
  assert.ok(body().textContent.includes(Logic.TEXT.shoppingUncheckedNote));
  await page.env.timers.advance(100);
  await page.settle();
  assert.equal(page.el("announcer").textContent, Logic.TEXT.shoppingSaved);
});

scenario("a_draft_can_be_cancelled_back_to_the_conversation_without_a_list", async () => {
  const page = await open(withStructuredRecipe(), { structured: true });
  page.articles()[1].buttons("Что купить?")[0].click();
  await page.settle();

  page.el("shopping-body").buttons("Отмена")[0].click();
  await page.settle();

  assert.deepEqual(["chat", "recipe", "shopping"].map(page.visible), [true, false, false]);
  assert.equal(page.el("tab-chats").getAttribute("aria-pressed"), "true");
  assert.ok(page.doc.activeElement === page.el("chat-title"));
  assert.equal(page.env.net.requests("POST", SHOP_URL).length, 0);
});

scenario("a_structured_favorite_offers_what_to_buy_and_a_legacy_one_does_not", async () => {
  const structured = H.structuredFavorite(H.FAV_1, 2, "Борщ", 0, 0, { version: 2 });
  const legacy = H.favorite(H.FAV_2, 12, "Плов", 30);
  const page = await open((env) => {
    oneConversation()(env);
    env.net.route("GET", "/api/favorites", H.favoritesBody([structured, legacy]));
    env.net.route(
      "GET",
      `/api/favorites/${H.FAV_1}`,
      H.favoriteDetail(structured, "Борщ", {
        servings: "4 порции",
        ingredients: SHOP_INGREDIENTS,
        steps: ["Сварите бульон."],
      })
    );
    env.net.route("GET", `/api/favorites/${H.FAV_2}`, H.favoriteDetail(legacy, "Плов: рис."));
  });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  assert.equal(page.el("recipe-body").buttons("Что купить?").length, 1);
  page.el("recipe-body").buttons("Что купить?")[0].click();
  await page.settle();
  assert.equal(page.visible("shopping"), true);
  assert.ok(page.el("shopping-body").textContent.includes(Logic.TEXT.shoppingDraftInstruction));

  page.el("shopping-body").buttons("Отмена")[0].click();
  await page.settle();
  assert.equal(page.visible("recipe"), true);
  assert.ok(page.doc.activeElement === page.el("recipe-title"));

  page.el("favorite-list").byTag("button")[1].click();
  await page.settle();
  assert.equal(page.el("recipe-body").buttons("Что купить?").length, 0, "nothing is inferred from a text favorite");
});

scenario("the_shopping_tab_lists_active_before_completed_and_opens_a_saved_list", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL_DONE, SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
    env.net.route("GET", SHOP_LIST_URL_DONE, json(200, SHOP_DETAIL_DONE));
  });

  page.el("tab-shopping").click();
  await page.settle();

  assert.deepEqual(["panel-chats", "panel-favorites", "panel-shopping", "chat", "recipe", "shopping"].map(page.visible), [
    false, false, true, false, false, true,
  ]);
  assert.equal(page.el("shopping-title").textContent, Logic.TEXT.shoppingTitle);
  const items = page.el("shopping-list").byTag("button");
  assert.deepEqual(items.map((button) => button.byClass("conv-label")[0].textContent), ["Борщ", "Плов"]);
  assert.ok(items[0].byClass("conv-meta")[0].textContent.includes("Куплено 1 из 3"));
  assert.ok(items[0].byClass("conv-meta")[0].textContent.includes("В работе"));
  assert.ok(items[1].byClass("conv-meta")[0].textContent.includes("Завершен"));
  assert.ok(page.el("shopping-body").textContent.includes(Logic.TEXT.shoppingPick));

  items[0].click();
  await page.settle();
  assert.equal(page.el("shopping-title").textContent, "Борщ");
  const body = page.el("shopping-body");
  assert.ok(body.textContent.includes("Куплено 1 из 3"));
  assert.deepEqual(body.byClass("shop-group-title").map((heading) => heading.textContent), ["Проверить / купить (1)", "Есть дома (1)", "Куплено (1)"]);
  assert.deepEqual(body.byTag("select").map((select) => select.value), ["unchecked", "at_home", "purchased"]);
  assert.equal(body.byTag("input")[2].getAttribute("checked"), "", "the purchased item is checked");
  assert.equal(page.el("shopping-list").byTag("button")[0].getAttribute("aria-current"), "true");
});

scenario("an_empty_shopping_section_says_where_lists_come_from", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, H.shoppingListsBody([]));
  });

  page.el("tab-shopping").click();
  await page.settle();

  assert.equal(page.el("shopping-status").textContent, "Пока нет списков покупок. Откройте рецепт и нажмите „Что купить?“");
  assert.ok(page.el("shopping-body").textContent.includes("Пока нет списков покупок."));
  assert.equal(page.el("shopping-list").byTag("button").length, 0);
});

scenario("a_shopping_checkbox_and_the_state_control_move_an_item_between_all_three_states", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  });
  page.env.net.route("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_2}`, json(200, H.shoppingItem(H.ITEM_2, 1, "мясо", "500 г", "purchased")), json(200, H.shoppingItem(H.ITEM_2, 1, "мясо", "500 г", "at_home")));
  page.env.net.route("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_3}`, json(200, H.shoppingItem(H.ITEM_3, 2, "сметана", "200 г", "unchecked")));
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  const item = () => page.el("shopping-body").find((element) => element.getAttribute("data-id") === H.ITEM_2)[0];
  const body = page.el("shopping-body");

  const checkbox = item().byTag("input")[0];
  page.doc.activeElement = checkbox;
  checkbox.checked = true;
  checkbox.fire("change");
  await page.settle();
  assert.deepEqual(page.env.net.requests("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_2}`)[0].body, { state: "purchased" });
  assert.equal(page.el("shopping-body").find((element) => element.getAttribute("data-id") === H.ITEM_2)[0].byTag("input")[0].getAttribute("checked"), "");
  assert.ok(page.el("shopping-body").find((element) => element.getAttribute("data-id") === H.ITEM_2)[0].byTag("input")[0] === page.doc.activeElement, "the focus stays on the item");

  const select = page.el("shopping-body").find((element) => element.getAttribute("data-id") === H.ITEM_2)[0].byTag("select")[0];
  select.value = "at_home";
  select.fire("change");
  await page.settle();
  assert.deepEqual(page.env.net.requests("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_2}`)[1].body, { state: "at_home" });
  assert.ok(page.el("shopping-body").textContent.includes("Есть дома: 2"));

  const purchased = page.el("shopping-body").find((element) => element.getAttribute("data-id") === H.ITEM_3)[0].byTag("input")[0];
  assert.equal(purchased.getAttribute("checked"), "");
  purchased.checked = false;
  purchased.fire("change");
  await page.settle();
  assert.deepEqual(page.env.net.requests("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_3}`)[0].body, { state: "unchecked" });
  assert.ok(page.el("shopping-body").textContent.includes("Куплено: 0"));
  assert.equal(body.scrollTop, 0);
});

scenario("a_completed_list_is_read_only_in_the_page_and_can_be_reopened", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL_DONE]));
    env.net.route("GET", SHOP_LIST_URL_DONE, json(200, SHOP_DETAIL_DONE));
  });
  page.env.net.route("PATCH", SHOP_LIST_URL_DONE, json(200, H.shoppingDetail(H.LIST_2, "Плов", "active", SHOP_DETAIL_DONE.items)));
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();

  const body = page.el("shopping-body");
  assert.ok(body.textContent.includes(Logic.TEXT.listCompletedNote));
  assert.equal(body.byClass("shop-item")[0].byTag("input")[0].getAttribute("disabled"), "");
  assert.equal(body.byClass("shop-item")[0].byTag("select")[0].getAttribute("disabled"), "");
  assert.equal(body.buttons("Изменить").length, 0);
  assert.equal(body.buttons("Добавить продукт").length, 0);
  assert.equal(body.buttons("Вернуть в работу").length, 1);

  body.buttons("Вернуть в работу")[0].click();
  await page.settle();
  assert.deepEqual(page.env.net.requests("PATCH", SHOP_LIST_URL_DONE)[0].body, { status: "active" });
  assert.ok(page.el("shopping-body").textContent.includes(Logic.TEXT.completeList));
  assert.equal(page.el("shopping-body").textContent.includes(Logic.TEXT.listCompletedNote), false);
});

scenario("deleting_a_list_asks_first_and_removes_only_that_list", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  });
  page.env.net.route("DELETE", SHOP_LIST_URL, H.removedBody());
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();

  page.el("shopping-body").buttons("Удалить список")[0].click();
  await page.settle();
  const confirm = page.el("shopping-body").byClass("confirm")[0];
  assert.ok(confirm.textContent.includes("Удалить список «Борщ»?"));
  assert.deepEqual(confirm.byTag("button").map((button) => button.textContent), ["Удалить", "Оставить"]);
  assert.equal(page.doc.activeElement.textContent, "Оставить");
  assert.equal(page.env.net.requests("DELETE", SHOP_LIST_URL).length, 0);

  page.el("shopping-body").byClass("confirm")[0].buttons("Оставить")[0].click();
  await page.settle();
  assert.equal(page.el("shopping-body").byClass("confirm").length, 0);
  assert.equal(page.env.net.requests("DELETE", SHOP_LIST_URL).length, 0);

  page.el("shopping-body").buttons("Удалить список")[0].click();
  await page.settle();
  page.el("shopping-body").byClass("confirm")[0].buttons("Удалить")[0].click();
  await page.settle();

  assert.equal(page.env.net.requests("DELETE", SHOP_LIST_URL).length, 1);
  assert.equal(page.el("shopping-list").byTag("button").length, 0);
  assert.ok(page.el("shopping-body").textContent.includes("Пока нет списков покупок."));
  await page.env.timers.advance(100);
  await page.settle();
  assert.equal(page.el("announcer").textContent, Logic.TEXT.listDeleted);
});

scenario("a_product_is_added_edited_and_removed_in_place_and_a_failed_edit_keeps_its_words", async () => {
  const added = H.shoppingItem("cccccccc-cccc-4ccc-8ccc-ccccccccccc4", 3, "соль", "1 ч. л.", "unchecked");
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  });
  page.env.net.route("POST", `${SHOP_LIST_URL}/items`, json(201, added));
  page.env.net.route("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_2}`, apiError(400, "invalid_request"), json(200, H.shoppingItem(H.ITEM_2, 1, "мясо на кости", "500 г", "unchecked")));
  page.env.net.route("DELETE", `${SHOP_LIST_URL}/items/${H.ITEM_2}`, H.removedBody());
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  const body = () => page.el("shopping-body");

  body().buttons("Добавить продукт")[0].click();
  await page.settle();
  assert.deepEqual([page.el("shop-add-name").getAttribute("id"), page.el("shop-add-amount").getAttribute("id")], ["shop-add-name", "shop-add-amount"]);
  page.editField(page.el("shop-add-name"), "  соль ");
  page.editField(page.el("shop-add-amount"), " 1 ч. л. ");
  body().byTag("form")[0].byTag("button")[0].click();
  await page.settle();
  assert.deepEqual(page.env.net.requests("POST", `${SHOP_LIST_URL}/items`)[0].body, { name: "соль", amount: "1 ч. л." });
  assert.ok(body().textContent.includes("соль"));

  body().buttons("Изменить").filter((button) => button.getAttribute("aria-label") === "Изменить: мясо")[0].click();
  await page.settle();
  const nameField = body().elements().filter((element) => element.getAttribute("data-focus") === "item-name-" + H.ITEM_2)[0];
  page.editField(nameField, "мясо на кости");
  body().byTag("form")[0].byTag("button")[0].click();
  await page.settle();
  assert.deepEqual(page.env.net.requests("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_2}`)[0].body, { name: "мясо на кости", amount: "500 г" });
  assert.ok(body().textContent.includes(Logic.CODE_MESSAGES.invalid_request));
  assert.equal(body().elements().filter((element) => element.getAttribute("data-focus") === "item-name-" + H.ITEM_2)[0].value, "мясо на кости", "the edit is not discarded");

  body().byTag("form")[0].byTag("button")[0].click();
  await page.settle();
  assert.ok(body().textContent.includes("мясо на кости"));

  body().buttons("Удалить").filter((button) => button.getAttribute("aria-label") === "Удалить: мясо на кости")[0].click();
  await page.settle();
  assert.ok(body().byClass("confirm")[0].textContent.includes(Logic.TEXT.itemRemoveAsk));
  body().byClass("confirm")[0].buttons("Удалить")[0].click();
  await page.settle();
  assert.equal(page.env.net.requests("DELETE", `${SHOP_LIST_URL}/items/${H.ITEM_2}`).length, 1);
  assert.equal(body().textContent.includes("мясо на кости"), false);
  assert.ok(page.el("shopping-title").textContent === "Борщ", "the list itself stays");
});

scenario("hostile_product_and_recipe_text_stays_text_in_the_shopping_pane", async () => {
  const hostile = '<img src=x onerror=alert(9)><script>alert(1)</script>';
  const page = await open(withStructuredRecipe(hostile, [{ name: hostile, amount: hostile }]), { structured: true });
  page.articles()[1].buttons("Что купить?")[0].click();
  await page.settle();

  const body = page.el("shopping-body");
  assert.ok(body.textContent.includes("<script>alert(1)</script>"));
  assert.deepEqual(["img", "script", "svg", "a", "iframe"].map((tag) => body.byTag(tag).length), [0, 0, 0, 0, 0]);
  // No attribute can run anything. (A label may NAME the hostile product -- the same inert way the
  // existing "open the kept recipe" control names its title -- but no value is ever a handler or an
  // address the page could load.)
  for (const [tag, name] of page.doc.attributeLog) {
    assert.ok(!/^on/i.test(name), `${tag}[${name}]`);
    assert.ok(!["style", "href", "src", "srcdoc", "action", "formaction"].includes(name), `${tag}[${name}]`);
  }
});

scenario("a_shopping_list_chosen_on_a_narrow_screen_closes_the_drawer", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  }, { narrow: true });
  page.el("drawer-toggle").click();
  page.el("tab-shopping").click();
  await page.settle();
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "open");
  assert.equal(page.doc.activeElement.getAttribute("id"), "tab-chats", "the drawer opens on the part that is in front");

  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "closed");
  assert.equal(page.el("shopping-title").textContent, "Борщ");
  assert.ok(page.doc.activeElement === page.el("shopping-title"));
});

scenario("overlapping_item_clicks_are_serialized_and_pending_clears", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  });
  const gate = deferred();
  page.env.net.route("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_2}`, async () => {
    await gate.promise;
    return apiError(400, "invalid_request");
  });
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  const item = (id) => page.el("shopping-body").find((element) => element.getAttribute("data-id") === id)[0];

  const first = item(H.ITEM_2).byTag("input")[0];
  first.checked = true;
  first.fire("change");
  await page.settle();
  assert.equal(item(H.ITEM_2).byTag("input")[0].getAttribute("disabled"), "", "the item being written is locked");
  assert.equal(item(H.ITEM_3).byTag("input")[0].getAttribute("disabled"), "", "so is every conflicting item");

  const second = item(H.ITEM_3).byTag("input")[0];
  second.checked = true;
  second.fire("change");
  await page.settle();
  assert.equal(page.env.net.requests("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_3}`).length, 0, "no overlapping write starts");

  gate.resolve();
  await page.settle();
  assert.equal(item(H.ITEM_2).byTag("input")[0].getAttribute("disabled"), null);
  assert.equal(item(H.ITEM_2).byTag("input")[0].getAttribute("checked"), null, "the refused change is taken back");
  assert.equal(item(H.ITEM_3).byTag("input")[0].getAttribute("disabled"), null);
  assert.ok(page.el("shopping-body").textContent.includes(Logic.CODE_MESSAGES.invalid_request));
});

scenario("a_modified_shopping_draft_is_not_left_without_a_question", async () => {
  const page = await open(withStructuredRecipe(), { structured: true });
  page.articles()[1].buttons("Что купить?")[0].click();
  await page.settle();
  const box = page.el("shopping-body").byTag("input")[0];
  box.checked = true;
  box.fire("change");
  await page.settle();

  page.env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
  page.env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  page.el("tab-shopping").click(); // reads the lists; the draft stays
  await page.settle();
  assert.equal(page.el("shopping-list").byTag("button").length, 1);

  let asked = 0;
  let answer = false;
  page.window.confirm = () => {
    asked++;
    return answer;
  };
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  assert.equal(asked, 1, "a modified draft is not discarded without a question");
  assert.equal(page.el("shopping-title").textContent, Logic.TEXT.shoppingDraftTitle);
  assert.equal(page.el("shopping-body").byTag("input")[0].getAttribute("checked"), "", "its mark is kept");
  assert.equal(page.env.net.requests("GET", SHOP_LIST_URL).length, 0, "a refused question navigates nowhere");
  assert.equal(page.env.net.requests("POST", SHOP_URL).length, 0, "nothing is saved or created");

  answer = true;
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  assert.equal(asked, 2);
  assert.equal(page.el("shopping-title").textContent, "Борщ");
  assert.equal(page.env.net.requests("GET", SHOP_LIST_URL).length, 1, "the confirmed question opens the list asked for");
});

scenario("a_dirty_shopping_draft_asks_before_the_mobile_drawer_navigates_away", async () => {
  const page = await open(withStructuredRecipe(), { structured: true, narrow: true });
  page.articles()[1].buttons("Что купить?")[0].click();
  await page.settle();
  const box = page.el("shopping-body").byTag("input")[0];
  box.checked = true;
  box.fire("change");
  await page.settle();

  page.env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
  page.env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  page.el("drawer-toggle").click();
  page.el("tab-shopping").click();
  await page.settle();
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "open");

  page.window.confirm = () => false;
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "open", "a refused question leaves the drawer as it was");
  assert.equal(page.el("shopping-title").textContent, Logic.TEXT.shoppingDraftTitle);
  assert.equal(page.el("shopping-body").byTag("input")[0].getAttribute("checked"), "");

  page.window.confirm = () => true;
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "closed");
  assert.equal(page.el("shopping-title").textContent, "Борщ");
});

scenario("a_modified_shopping_draft_asks_the_browser_before_a_refresh_or_a_close", async () => {
  // beforeunload is the browser's own question. The page cannot force it to be shown (a real
  // browser decides that), so what is checked here is the only part it controls: preventDefault
  // is called exactly while the draft holds a modification. Both the wide and the narrow
  // layouts are checked; the drawing differs, the protection does not.
  for (const narrow of [false, true]) {
    const page = await open(withStructuredRecipe(), { structured: true, narrow });
    const prevented = () => {
      let yes = false;
      page.fireWindow("beforeunload", {
        preventDefault() {
          yes = true;
        },
      });
      return yes;
    };

    page.articles()[1].buttons("Что купить?")[0].click();
    await page.settle();
    assert.equal(prevented(), false, "an untouched draft is not unsaved work");

    const box = page.el("shopping-body").byTag("input")[0];
    box.checked = true;
    box.fire("change");
    await page.settle();
    assert.equal(prevented(), true, "a mark is an unsaved modification");

    // The draft's own in-app question is still asked, and a refused one keeps the draft.
    let asked = 0;
    page.window.confirm = () => {
      asked++;
      return false;
    };
    page.articles()[1].buttons("Что купить?")[0].click();
    await page.settle();
    assert.equal(asked, 1);
    assert.equal(page.el("shopping-title").textContent, Logic.TEXT.shoppingDraftTitle);
    assert.equal(prevented(), true);

    // Explicitly discarded: nothing is left to warn about.
    page.el("shopping-body").buttons(Logic.TEXT.shoppingCancel)[0].click();
    await page.settle();
    assert.equal(prevented(), false, "a discarded draft is not unsaved work");

    // Saved: the draft becomes a list, and the question is gone.
    page.env.net.route("POST", SHOP_URL, json(201, SHOP_DETAIL));
    page.articles()[1].buttons("Что купить?")[0].click();
    await page.settle();
    const again = page.el("shopping-body").byTag("input")[0];
    again.checked = true;
    again.fire("change");
    await page.settle();
    assert.equal(prevented(), true);
    page.el("shopping-body").buttons(Logic.TEXT.shoppingSave)[0].click();
    await page.settle();
    assert.equal(page.el("shopping-title").textContent, SHOP_DETAIL.title);
    assert.equal(prevented(), false, "a saved draft is not unsaved work");
  }
});

scenario("a_draft_whose_save_is_out_or_in_doubt_is_protected_until_it_is_settled", async () => {
  // A refresh or a close while a save is still out -- or while the draft of a save whose answer
  // was lost waits for its retry -- would take the marked ingredients and the remembered key
  // with it. The browser's own question is kept until the save is confirmed or the person
  // discards the draft. (A mobile browser may still suppress that question; this checks only
  // what the page asks of it.)
  const configure = (env) => {
    withStructuredRecipe()(env);
    const item = structuredFavoriteItem();
    env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
    env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ\n\n1. Сварите бульон."));
  };
  const page = await open(configure, { structured: true });
  const prevented = () => {
    let yes = false;
    page.fireWindow("beforeunload", {
      preventDefault() {
        yes = true;
      },
    });
    return yes;
  };
  const openDraft = async () => {
    page.el("tab-chats").click();
    await page.settle();
    page.articles()[1].buttons("Что купить?")[0].click();
    await page.settle();
  };
  const markOne = async () => {
    const box = page.el("shopping-body").byTag("input")[0];
    box.checked = true;
    box.fire("change");
    await page.settle();
  };

  await openDraft();
  assert.equal(prevented(), false, "an untouched draft is not unsaved work");
  await markOne();
  assert.equal(prevented(), true, "a mark is an unsaved modification");

  // The save is out and nothing has answered yet: the draft and its key must not be lost.
  const gate = deferred();
  page.env.net.route("POST", SHOP_URL, async () => {
    await gate.promise;
    return json(201, SHOP_DETAIL);
  });
  page.el("shopping-body").buttons(Logic.TEXT.shoppingSave)[0].click();
  await page.settle();
  assert.equal(page.env.net.requests("POST", SHOP_URL).length, 1);
  assert.equal(page.el("shopping-body").buttons(Logic.TEXT.shoppingSaving).length, 1, "the save is under way");
  assert.equal(prevented(), true, "an outstanding save is not settled work");

  gate.resolve();
  await page.settle();
  assert.equal(page.el("shopping-title").textContent, SHOP_DETAIL.title);
  assert.equal(prevented(), false, "a confirmed save is not unsaved work");

  // Its answer is lost instead: the draft is kept, with the same key, for an explicit retry.
  await openDraft();
  await markOne();
  page.env.net.route("POST", SHOP_URL, fail());
  page.el("shopping-body").buttons(Logic.TEXT.shoppingSave)[0].click();
  await page.settle();
  assert.ok(page.el("shopping-body").textContent.includes(Logic.TEXT.shoppingUncertain));
  assert.equal(prevented(), true, "a save of an unknown outcome is still unsaved work");

  page.env.net.route("POST", SHOP_URL, json(200, { ...SHOP_DETAIL, created: false }));
  page.el("shopping-body").buttons(Logic.TEXT.shoppingRetrySave)[0].click();
  await page.settle();
  const keys = page.env.net.requests("POST", SHOP_URL).map((request) => request.headers["idempotency-key"]);
  assert.equal(keys.length, 3);
  assert.equal(keys[1], keys[2], "the retry sends the same key");
  assert.equal(page.el("shopping-title").textContent, SHOP_DETAIL.title);
  assert.equal(prevented(), false, "the confirmed retry ends the question");

  // Explicitly discarded after an unclear save: nothing is left to warn about.
  await openDraft();
  await markOne();
  page.env.net.route("POST", SHOP_URL, fail());
  page.el("shopping-body").buttons(Logic.TEXT.shoppingSave)[0].click();
  await page.settle();
  assert.equal(prevented(), true);
  page.el("shopping-body").buttons(Logic.TEXT.shoppingCancel)[0].click();
  await page.settle();
  assert.equal(prevented(), false, "a discarded draft is not unsaved work");

  // The favorites editor keeps its own protection beside all of this.
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  page.el("recipe-body").buttons(Logic.TEXT.editAction)[0].click();
  await page.settle();
  assert.equal(prevented(), true, "an open editor is still unsaved work");
  page.el("recipe-body").buttons(Logic.TEXT.editCancel)[0].click();
  await page.settle();
  assert.equal(prevented(), false);
});

scenario("an_unmarked_draft_is_protected_while_its_save_is_out_or_in_doubt", async () => {
  // Nothing is marked, but a save that is out, or one whose answer was lost, holds the key that
  // is the only way to learn whether the list was made: the browser's own question is kept for
  // it exactly as for a marked draft.
  const page = await open(withStructuredRecipe(), { structured: true });
  const prevented = () => {
    let yes = false;
    page.fireWindow("beforeunload", {
      preventDefault() {
        yes = true;
      },
    });
    return yes;
  };
  const openDraft = async () => {
    page.el("tab-chats").click();
    await page.settle();
    page.articles()[1].buttons("Что купить?")[0].click();
    await page.settle();
  };

  await openDraft();
  assert.equal(prevented(), false, "an untouched draft is not unsaved work");
  assert.equal(
    page.el("shopping-body").byTag("input").filter((box) => box.getAttribute("checked") === "").length,
    0,
    "nothing is marked"
  );

  // The save is out: the unmarked draft is still work that would be lost with the page.
  const gate = deferred();
  page.env.net.route("POST", SHOP_URL, async () => {
    await gate.promise;
    return json(201, SHOP_DETAIL);
  });
  page.el("shopping-body").buttons(Logic.TEXT.shoppingSave)[0].click();
  await page.settle();
  assert.equal(page.el("shopping-body").buttons(Logic.TEXT.shoppingSaving).length, 1, "the save is under way");
  assert.equal(prevented(), true, "an unmarked save that is out is not settled work");

  gate.resolve();
  await page.settle();
  assert.equal(page.el("shopping-title").textContent, SHOP_DETAIL.title);
  assert.equal(prevented(), false, "a confirmed save is not unsaved work");

  // Its answer is lost instead, again with nothing marked.
  await openDraft();
  page.env.net.route("POST", SHOP_URL, fail());
  page.el("shopping-body").buttons(Logic.TEXT.shoppingSave)[0].click();
  await page.settle();
  assert.ok(page.el("shopping-body").textContent.includes(Logic.TEXT.shoppingUncertain));
  assert.equal(prevented(), true, "an unmarked save of an unknown outcome is still unsaved work");

  page.el("shopping-body").buttons(Logic.TEXT.shoppingCancel)[0].click();
  await page.settle();
  assert.equal(page.el("shopping-body").byTag("input").length, 0, "the discarded draft is gone from the page");
  assert.equal(prevented(), false, "an explicitly discarded draft is not unsaved work");

  // A session that is over holds nothing of the unmarked draft, its save included.
  await openDraft();
  const late = deferred();
  page.env.net.route("POST", SHOP_URL, async () => {
    await late.promise;
    return json(201, SHOP_DETAIL);
  });
  page.el("shopping-body").buttons(Logic.TEXT.shoppingSave)[0].click();
  await page.settle();
  assert.equal(prevented(), true);
  page.env.net.route("GET", "/auth/me", notAuthenticated());
  page.doc.visibilityState = "visible";
  page.doc.fire("visibilitychange");
  await page.settle();
  assert.equal(page.visible("screen-login"), true);
  assert.equal(prevented(), false, "nothing of the ended session is unsaved work");
  late.resolve();
  await page.settle();
  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.el("shopping-body").childNodes.length, 0, "the late answer does not bring the draft back");
  assert.equal(prevented(), false);
});

scenario("a_draft_of_a_session_that_is_over_asks_nothing_and_a_late_save_changes_nothing", async () => {
  const page = await open(withStructuredRecipe(), { structured: true });
  const prevented = () => {
    let yes = false;
    page.fireWindow("beforeunload", {
      preventDefault() {
        yes = true;
      },
    });
    return yes;
  };

  page.articles()[1].buttons("Что купить?")[0].click();
  await page.settle();
  const box = page.el("shopping-body").byTag("input")[0];
  box.checked = true;
  box.fire("change");
  await page.settle();
  assert.equal(prevented(), true);

  // The save is still out when the session ends: everything of the person is dropped at once,
  // the draft and its key included, and the answer that comes later brings nothing back.
  const gate = deferred();
  page.env.net.route("POST", SHOP_URL, async () => {
    await gate.promise;
    return json(201, SHOP_DETAIL);
  });
  page.el("shopping-body").buttons(Logic.TEXT.shoppingSave)[0].click();
  await page.settle();
  assert.equal(page.el("shopping-body").buttons(Logic.TEXT.shoppingSaving).length, 1);

  page.env.net.route("GET", "/auth/me", notAuthenticated());
  page.doc.visibilityState = "visible";
  page.doc.fire("visibilitychange");
  await page.settle();
  assert.equal(page.visible("screen-login"), true);
  assert.equal(prevented(), false, "nothing of the ended session is unsaved work");

  gate.resolve();
  await page.settle();
  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.el("shopping-body").childNodes.length, 0, "the late answer does not bring the draft back");
  assert.equal(prevented(), false);
});

scenario("the_mobile_drawer_makes_the_shopping_pane_inert_and_restores_it", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  }, { narrow: true });
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  assert.deepEqual(["chat", "recipe", "shopping"].map((id) => page.el(id).inert), [false, false, false]);

  page.el("drawer-toggle").click();
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "open");
  assert.deepEqual(["chat", "recipe", "shopping"].map((id) => page.el(id).inert), [true, true, true], "nothing behind the drawer takes a focus or a click");

  page.el("backdrop").click();
  await page.settle();
  assert.equal(page.el("screen-app").getAttribute("data-drawer"), "closed");
  assert.deepEqual(["chat", "recipe", "shopping"].map((id) => page.el(id).inert), [false, false, false]);
});

scenario("deleting_one_list_while_another_is_opening_leaves_the_other_usable", async () => {
  const plov = H.shoppingDetail(H.LIST_2, "Плов", "active", [
    H.shoppingItem("cccccccc-cccc-4ccc-8ccc-ccccccccccc4", 0, "рис", "200 г", "unchecked"),
  ]);
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL, plov]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  });
  const deleteGate = deferred();
  const readGate = deferred();
  page.env.net.route("DELETE", SHOP_LIST_URL, async () => {
    await deleteGate.promise;
    return H.removedBody();
  });
  page.env.net.route("GET", SHOP_LIST_URL_DONE, async () => {
    await readGate.promise;
    return json(200, plov);
  });

  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click(); // Борщ
  await page.settle();
  page.el("shopping-body").buttons(Logic.TEXT.deleteList)[0].click();
  await page.settle();
  page.el("shopping-body").byClass("confirm")[0].buttons(Logic.TEXT.deleteListConfirm)[0].click();
  await page.settle();
  assert.equal(page.env.net.requests("DELETE", SHOP_LIST_URL).length, 1);

  // B is not blocked by A's unsettled write; its read is let out while A's answer is still out.
  const plovButton = page.el("shopping-list").byTag("button")[1];
  assert.equal(plovButton.getAttribute("disabled"), null);
  plovButton.click();
  await page.settle();
  assert.ok(page.el("shopping-body").textContent.includes(Logic.TEXT.shoppingListLoading));

  deleteGate.resolve();
  await page.settle();
  assert.ok(
    page.el("shopping-body").textContent.includes(Logic.TEXT.shoppingListLoading),
    "B keeps waiting for B's own answer, not for A's deletion"
  );

  readGate.resolve();
  await page.settle();
  assert.equal(page.el("shopping-title").textContent, "Плов");
  assert.equal(
    page.el("shopping-body").textContent.includes(Logic.TEXT.shoppingListLoading),
    false,
    "B's answer is applied even though A's deletion finished first"
  );
  assert.ok(page.el("shopping-body").textContent.includes("рис"));
  assert.equal(page.el("shopping-list").byTag("button").length, 1, "A is gone from the summaries");
  assert.equal(page.el("shopping-body").buttons(Logic.TEXT.addItem).length, 1, "B can still be worked with");
});

scenario("an_unclear_product_add_keeps_the_form_and_its_words", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL), json(200, SHOP_DETAIL));
  });
  page.env.net.route("POST", `${SHOP_LIST_URL}/items`, fail());
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  const body = () => page.el("shopping-body");

  body().buttons("Добавить продукт")[0].click();
  await page.settle();
  page.editField(page.el("shop-add-name"), "соль");
  page.editField(page.el("shop-add-amount"), "1 ч. л.");
  body().byTag("form")[0].byTag("button")[0].click();
  await page.settle();

  assert.equal(page.env.net.requests("POST", `${SHOP_LIST_URL}/items`).length, 1, "nothing is sent again by itself");
  assert.equal(body().byClass("shop-add").length, 1, "the form is still there");
  assert.deepEqual([page.el("shop-add-name").value, page.el("shop-add-amount").value], ["соль", "1 ч. л."]);
  assert.ok(body().textContent.includes(Logic.TEXT.addItemUncertain));
  assert.equal(
    body().byTag("form")[0].byTag("button")[0].getAttribute("disabled"),
    null,
    "an active list still offers the explicit retry"
  );
  assert.equal(body().elements().filter((element) => element.getAttribute("data-id") === H.ITEM_2).length, 1, "the list itself stays");

  body().buttons("Отмена")[0].click();
  await page.settle();
  assert.equal(body().byTag("form").length, 0);
  assert.equal(body().buttons("Добавить продукт").length, 1);
  assert.equal(page.env.net.requests("POST", `${SHOP_LIST_URL}/items`).length, 1);
});

scenario("an_unclear_add_over_a_completed_list_stays_visible_and_reopens_before_sending", async () => {
  // The review's reproduction: the add's answer is lost and the true list turns out to be
  // completed without the item. The form must not vanish (it holds the only copy of the words
  // and the only cancellation), and «Вернуть в работу» must work; only after that may the
  // retained values be sent again, by an explicit click.
  const completed = H.shoppingDetail(H.LIST_1, "Борщ", "completed", SHOP_ITEMS);
  const added = H.shoppingItem("cccccccc-cccc-4ccc-8ccc-ccccccccccc4", 3, "соль", "1 ч. л.", "unchecked");
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL), json(200, completed));
  });
  page.env.net.route("POST", `${SHOP_LIST_URL}/items`, fail());
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  const body = () => page.el("shopping-body");

  body().buttons(Logic.TEXT.addItem)[0].click();
  await page.settle();
  page.editField(page.el("shop-add-name"), "соль");
  page.editField(page.el("shop-add-amount"), "1 ч. л.");
  body().byTag("form")[0].byTag("button")[0].click();
  await page.settle();

  assert.equal(page.env.net.requests("POST", `${SHOP_LIST_URL}/items`).length, 1, "nothing is sent again by itself");
  assert.ok(body().textContent.includes(Logic.TEXT.listCompletedNote), "the list is completed now");
  assert.ok(body().textContent.includes(Logic.TEXT.addItemUncertain), "the warning is not hidden by the completion");
  assert.deepEqual([page.el("shop-add-name").value, page.el("shop-add-amount").value], ["соль", "1 ч. л."]);
  assert.equal(page.el("shop-add-name").getAttribute("readonly"), "");
  assert.equal(
    body().byTag("form")[0].byTag("button")[0].getAttribute("disabled"),
    "",
    "a completed list sends no new item"
  );
  assert.equal(body().buttons(Logic.TEXT.addItemCancel).length, 1, "the unresolved addition can be cancelled");
  const reopen = body().buttons(Logic.TEXT.reopenList)[0];
  assert.ok(reopen, "«Вернуть в работу» is offered");
  assert.equal(reopen.getAttribute("disabled"), null, "and is not refused by the hidden form");

  page.env.net.route("PATCH", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  reopen.click();
  await page.settle();
  assert.equal(page.env.net.requests("PATCH", SHOP_LIST_URL).length, 1);
  assert.equal(page.env.net.requests("POST", `${SHOP_LIST_URL}/items`).length, 1, "reopening retries nothing");
  assert.equal(body().textContent.includes(Logic.TEXT.listCompletedNote), false);
  assert.ok(body().textContent.includes(Logic.TEXT.addItemUncertain), "the unresolved warning stays");
  assert.deepEqual([page.el("shop-add-name").value, page.el("shop-add-amount").value], ["соль", "1 ч. л."]);
  const submit = body().byTag("form")[0].byTag("button")[0];
  assert.equal(submit.getAttribute("disabled"), null, "the retained values can now be sent explicitly");

  page.env.net.route("POST", `${SHOP_LIST_URL}/items`, json(201, added));
  submit.click();
  await page.settle();
  assert.equal(page.env.net.requests("POST", `${SHOP_LIST_URL}/items`).length, 2, "the person's click is the second send");
  assert.equal(body().byTag("form").length, 0, "the resolved form is gone");
  assert.ok(body().textContent.includes("соль"));
  assert.ok(body().textContent.includes("Куплено 1 из 4"));
});

scenario("an_unresolved_add_can_be_cancelled_from_a_completed_list", async () => {
  const completed = H.shoppingDetail(H.LIST_1, "Борщ", "completed", SHOP_ITEMS);
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL), json(200, completed));
  });
  page.env.net.route("POST", `${SHOP_LIST_URL}/items`, fail());
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  const body = () => page.el("shopping-body");

  body().buttons(Logic.TEXT.addItem)[0].click();
  await page.settle();
  page.editField(page.el("shop-add-name"), "соль");
  body().byTag("form")[0].byTag("button")[0].click();
  await page.settle();
  assert.ok(body().textContent.includes(Logic.TEXT.addItemUncertain));

  body().buttons(Logic.TEXT.addItemCancel)[0].click();
  await page.settle();
  assert.equal(body().byTag("form").length, 0, "the hidden blocking form is gone");
  assert.equal(page.env.net.requests("POST", `${SHOP_LIST_URL}/items`).length, 1, "cancelling sends nothing");
  assert.ok(body().textContent.includes(Logic.TEXT.listCompletedNote), "the completed list itself stays");
  assert.equal(
    body().elements().filter((element) => element.getAttribute("data-id") === H.ITEM_2).length,
    1,
    "its items are untouched"
  );

  // With nothing unresolved left, the list opens for work normally.
  page.env.net.route("PATCH", SHOP_LIST_URL, json(200, SHOP_DETAIL));
  body().buttons(Logic.TEXT.reopenList)[0].click();
  await page.settle();
  assert.equal(body().textContent.includes(Logic.TEXT.listCompletedNote), false);
  assert.equal(body().buttons(Logic.TEXT.addItem).length, 1);
});

scenario("an_item_with_a_position_beyond_the_item_count_is_drawn_and_editable", async () => {
  const atCap = H.shoppingItem("cccccccc-cccc-4ccc-8ccc-ccccccccccc4", 200, "соль", "1 ч. л.", "unchecked");
  const detail = H.shoppingDetail(H.LIST_1, "Борщ", "active", SHOP_ITEMS.concat([atCap]));
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([detail]));
    env.net.route("GET", SHOP_LIST_URL, json(200, detail));
  });
  page.env.net.route("PATCH", `${SHOP_LIST_URL}/items/${atCap.id}`, json(200, { ...atCap, state: "purchased" }));
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();

  const row = page.el("shopping-body").find((element) => element.getAttribute("data-id") === atCap.id)[0];
  assert.ok(row, "the item at position 200 is drawn");
  assert.ok(row.textContent.includes("соль"));
  const checkbox = row.byTag("input")[0];
  checkbox.checked = true;
  checkbox.fire("change");
  await page.settle();
  assert.deepEqual(page.env.net.requests("PATCH", `${SHOP_LIST_URL}/items/${atCap.id}`)[0].body, { state: "purchased" });
  assert.ok(page.el("shopping-body").textContent.includes("Куплено: 2"));
});

scenario("a_reused_key_says_so_and_a_new_list_needs_the_persons_click", async () => {
  const page = await open(withStructuredRecipe(), { structured: true });
  page.env.net.route("POST", SHOP_URL, apiError(409, "shopping_list_key_reused"), json(201, SHOP_DETAIL));
  page.articles()[1].buttons("Что купить?")[0].click();
  await page.settle();
  const body = () => page.el("shopping-body");

  body().buttons("Сохранить список")[0].click();
  await page.settle();
  assert.ok(body().textContent.includes(Logic.TEXT.shoppingKeyReused));
  assert.equal(body().buttons("Сохранить новый список").length, 1);
  assert.equal(page.env.net.requests("POST", SHOP_URL).length, 1, "nothing is sent by itself");

  body().buttons("Сохранить новый список")[0].click();
  await page.settle();
  assert.equal(page.env.net.requests("POST", SHOP_URL).length, 2);
  assert.equal(page.el("shopping-title").textContent, "Борщ");
  const keys = page.env.net.requests("POST", SHOP_URL).map((request) => request.headers["idempotency-key"]);
  assert.equal(new Set(keys).size, 2, "the explicit new save made a new key");
});

scenario("a_deleted_lists_key_is_terminal_and_the_person_is_told", async () => {
  const page = await open(withStructuredRecipe(), { structured: true });
  page.env.net.route("POST", SHOP_URL, apiError(409, "shopping_list_deleted"));
  page.articles()[1].buttons("Что купить?")[0].click();
  await page.settle();

  page.el("shopping-body").buttons("Сохранить список")[0].click();
  await page.settle();
  assert.ok(page.el("shopping-body").textContent.includes(Logic.TEXT.shoppingDeletedElsewhere));
  assert.equal(page.el("shopping-body").buttons("Сохранить новый список").length, 0);
  assert.equal(page.el("shopping-body").buttons("Повторить").length, 0);
  assert.equal(page.el("shopping-body").buttons("Понятно").length, 1);

  page.el("shopping-body").buttons("Понятно")[0].click();
  await page.settle();
  assert.equal(page.visible("chat"), true);
  assert.equal(page.env.net.requests("POST", SHOP_URL).length, 1);
});

scenario("an_unclear_save_offers_the_same_key_again_and_nothing_else", async () => {
  const page = await open(withStructuredRecipe(), { structured: true });
  page.env.net.route("POST", SHOP_URL, fail(), json(200, { ...SHOP_DETAIL, created: false }));
  page.articles()[1].buttons("Что купить?")[0].click();
  await page.settle();
  page.el("shopping-body").byTag("input")[0].checked = true;
  page.el("shopping-body").byTag("input")[0].fire("change");
  await page.settle();

  page.el("shopping-body").buttons("Сохранить список")[0].click();
  await page.settle();
  assert.ok(page.el("shopping-body").textContent.includes(Logic.TEXT.shoppingUncertain));
  assert.equal(page.el("shopping-body").byTag("input")[0].getAttribute("disabled"), "");
  assert.equal(page.el("shopping-body").buttons("Повторить").length, 1);
  assert.equal(page.el("shopping-body").buttons("Сохранить новый список").length, 0);

  page.el("shopping-body").buttons("Повторить")[0].click();
  await page.settle();
  const requests = page.env.net.requests("POST", SHOP_URL);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].headers["idempotency-key"], requests[1].headers["idempotency-key"]);
  assert.equal(page.el("shopping-title").textContent, "Борщ");
});

scenario("a_session_that_ends_takes_the_shopping_lists_and_drafts_off_the_page", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", SHOP_URL, shoppingLists([SHOP_DETAIL]));
    env.net.route("GET", SHOP_LIST_URL, json(200, SHOP_DETAIL));
    env.net.route("GET", "/auth/me", me(30));
  });
  page.el("tab-shopping").click();
  await page.settle();
  page.el("shopping-list").byTag("button")[0].click();
  await page.settle();
  page.env.net.route("PATCH", `${SHOP_LIST_URL}/items/${H.ITEM_2}`, notAuthenticated());
  const checkbox = page.el("shopping-body").find((element) => element.getAttribute("data-id") === H.ITEM_2)[0].byTag("input")[0];
  checkbox.checked = true;
  checkbox.fire("change");
  await page.settle();

  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.visible("screen-app"), false);
  assert.equal(page.el("shopping-body").childNodes.length, 0);
  assert.equal(page.el("shopping-list").childNodes.length, 0);
  assert.equal(page.el("shopping-title").textContent, "");
  assert.ok(!page.root().includes("Борщ"));
  assert.ok(!page.root().includes("мясо"));
});

// ============================================================================================
// The profile
// ============================================================================================

scenario("the_profile_button_opens_its_own_pane_with_the_saved_values_the_ways_in_and_the_data_note", async () => {
  const page = await open();
  assert.equal(page.visible("profile"), false);

  page.el("profile-open").click();
  await page.settle();
  await page.settle();

  assert.equal(page.visible("screen-app"), true);
  assert.equal(page.visible("profile"), true);
  assert.equal(page.visible("chat"), false);
  assert.equal(page.visible("recipe"), false);
  assert.equal(page.visible("shopping"), false);
  assert.equal(page.el("profile-open").getAttribute("aria-pressed"), "true");
  assert.equal(page.el("profile-title").textContent, "Профиль");
  assert.equal(page.doc.activeElement, page.el("profile-title"));

  // The saved settings are values to read, not fields to edit: «Изменить» is the way in.
  const body = page.el("profile-body");
  assert.ok(body.textContent.includes("Елена"));
  assert.ok(body.textContent.includes(Logic.TEXT.profileSizeNormal));
  assert.equal(
    body.byTag("input").length,
    0,
    "no name field and no text-size choice before «Изменить»"
  );
  assert.equal(body.buttons(Logic.TEXT.profileEdit).length, 1);
  assert.equal(body.buttons(Logic.TEXT.profileSave).length, 0);
  assert.equal(body.buttons(Logic.TEXT.profileEdit)[0].getAttribute("type"), "button");
  assert.ok(body.textContent.includes("Отображаемое имя"));
  assert.ok(body.textContent.includes("Способы входа"));
  assert.ok(body.textContent.includes("Telegram") && body.textContent.includes("Активен"));
  assert.ok(body.textContent.includes("Ваши данные"));
  assert.ok(body.textContent.includes("365"));
  assert.ok(body.textContent.includes("OpenAI"));
  assert.ok(body.textContent.includes("избранное"));
  assert.ok(body.textContent.includes("списки покупок"));

  // «Изменить» opens the form on the saved values, and the keyboard goes to the name.
  body.buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();

  assert.equal(page.el("profile-name").value, "Елена");
  const radios = page.el("profile-body").find((element) => element.getAttribute("type") === "radio");
  assert.deepEqual(radios.map((radio) => radio.getAttribute("value")), ["normal", "large"]);
  assert.equal(radios[0].checked, true);
  assert.equal(page.el("profile-body").buttons(Logic.TEXT.profileSave).length, 1);
  assert.equal(page.el("profile-body").buttons(Logic.TEXT.profileEdit).length, 0);
  assert.equal(page.doc.activeElement, page.el("profile-name"));
});

scenario("the_large_text_size_is_applied_to_the_signed_in_page_and_comes_off_on_the_way_out", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", "/api/profile", H.profileBody("Елена", "large"));
  });
  await page.settle();

  assert.equal(page.doc.documentElement.getAttribute("data-text-size"), "large");
  page.el("profile-open").click();
  await page.settle();
  assert.ok(page.el("profile-body").textContent.includes(Logic.TEXT.profileSizeLarge));
  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  const large = page
    .el("profile-body")
    .find((element) => element.getAttribute("type") === "radio" && element.getAttribute("value") === "large")[0];
  assert.equal(large.checked, true);

  page.env.net.route("POST", "/auth/logout", text(204, null));
  page.el("logout").click();
  await page.settle();
  await page.settle();

  assert.equal(page.visible("screen-login"), true);
  assert.equal(page.doc.documentElement.getAttribute("data-text-size"), null);
});

scenario("a_profile_that_cannot_be_read_leaves_the_conversations_working", async () => {
  const page = await open((env) => {
    signedInWith([conversation(CONV_A, 0, "Старый разговор")], {
      [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]),
    })(env);
    env.net.route("GET", "/api/profile", fail(), fail(), H.profileBody("Елена", "normal"));
  });
  await page.env.ctl.idle();
  await page.settle();

  assert.equal(page.visible("screen-app"), true);
  assert.ok(page.el("conversation-list").textContent.includes("Старый разговор"));
  assert.equal(page.el("composer-input").readOnly, false);

  page.env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Новый вопрос", 4, "Ответ"));
  await page.send("Новый вопрос");
  assert.ok(page.articleTexts().some((text) => text.includes("Ответ")));

  page.el("profile-open").click();
  await page.settle();
  await page.settle();
  assert.equal(page.visible("profile"), true);
  assert.ok(page.el("profile-body").textContent.includes(Logic.TEXT.network));
  assert.ok(page.el("profile-body").textContent.includes(Logic.TEXT.profileOthersWork));
  assert.equal(page.el("profile-body").buttons("Повторить").length, 1);

  page.el("profile-body").buttons("Повторить")[0].click();
  await page.settle();
  await page.settle();
  assert.ok(page.el("profile-body").textContent.includes("Елена"));
  assert.equal(page.el("profile-body").byTag("input").length, 0, "the pane opens on the saved values");
  assert.ok(!page.el("profile-body").textContent.includes(Logic.TEXT.profileFailed));
});

scenario("saving_returns_to_the_saved_values_and_izmenit_opens_the_form_again", async () => {
  const page = await open();
  page.el("profile-open").click();
  await page.settle();
  await page.settle();

  // First change: the name is trimmed by the server, the size becomes large.
  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  page.editField(page.el("profile-name"), "  Иван  ");
  const large = page
    .el("profile-body")
    .find((element) => element.getAttribute("type") === "radio" && element.getAttribute("value") === "large")[0];
  large.checked = true;
  large.fire("change");
  page.env.net.route("PATCH", "/api/profile", H.profileBody("Иван", "large"));
  const saveButton = page.el("profile-body").buttons(Logic.TEXT.profileSave)[0];
  saveButton.focus(); // a real click leaves the keyboard on the button
  saveButton.click();
  await page.settle();
  await page.settle();

  const requests = page.env.net.requests("PATCH", "/api/profile");
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body, { display_name: "Иван", text_size: "large" });

  // The answer is what the pane shows: the saved values, the save's message, «Изменить» back.
  const body = page.el("profile-body");
  assert.ok(body.textContent.includes(Logic.TEXT.profileSaved));
  assert.ok(body.textContent.includes("Иван"));
  assert.ok(body.textContent.includes(Logic.TEXT.profileSizeLarge));
  assert.equal(page.el("profile-body").byTag("input").length, 0, "the form is closed");
  assert.equal(body.buttons(Logic.TEXT.profileSave).length, 0);
  assert.equal(body.buttons(Logic.TEXT.profileEdit).length, 1);
  assert.equal(page.doc.documentElement.getAttribute("data-text-size"), "large");
  assert.equal(page.doc.activeElement, body.buttons(Logic.TEXT.profileEdit)[0], "the keyboard stays in the pane");

  // Reopening the form shows the saved values, and a second change is a second save.
  body.buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  assert.equal(page.el("profile-name").value, "Иван");
  const largeAgain = page
    .el("profile-body")
    .find((element) => element.getAttribute("type") === "radio" && element.getAttribute("value") === "large")[0];
  assert.equal(largeAgain.checked, true);
  assert.ok(!page.el("profile-body").textContent.includes(Logic.TEXT.profileSaved), "the old message is gone");

  page.editField(page.el("profile-name"), "Иван Петров");
  // The mini DOM does not group radios: uncheck the other one as the browser would.
  largeAgain.checked = false;
  const normalAgain = page
    .el("profile-body")
    .find((element) => element.getAttribute("type") === "radio" && element.getAttribute("value") === "normal")[0];
  normalAgain.checked = true;
  normalAgain.fire("change");
  page.env.net.route("PATCH", "/api/profile", H.profileBody("Иван Петров", "normal"));
  page.el("profile-body").buttons(Logic.TEXT.profileSave)[0].click();
  await page.settle();
  await page.settle();

  assert.equal(page.env.net.requests("PATCH", "/api/profile").length, 2);
  assert.deepEqual(page.env.net.requests("PATCH", "/api/profile")[1].body, {
    display_name: "Иван Петров",
    text_size: "normal",
  });
  assert.ok(page.el("profile-body").textContent.includes("Иван Петров"));
  assert.ok(page.el("profile-body").textContent.includes(Logic.TEXT.profileSizeNormal));
  assert.equal(page.doc.documentElement.getAttribute("data-text-size"), null);
});

scenario("a_form_that_holds_the_saved_values_sends_no_patch_and_returns_to_them", async () => {
  const page = await open();
  page.el("profile-open").click();
  await page.settle();
  await page.settle();
  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();

  // Space around the saved name is not a change: the server would trim it away.
  page.editField(page.el("profile-name"), "  Елена  ");
  page.el("profile-body").buttons(Logic.TEXT.profileSave)[0].click();
  await page.settle();
  await page.settle();

  assert.equal(page.env.net.requests("PATCH", "/api/profile").length, 0, "nothing was written");
  assert.equal(page.el("profile-body").byTag("input").length, 0, "the form closed on the unchanged values");
  assert.ok(page.el("profile-body").textContent.includes("Елена"));
  assert.ok(!page.el("profile-body").textContent.includes(Logic.TEXT.profileSaved), "no save is claimed");
  assert.equal(page.el("profile-body").buttons(Logic.TEXT.profileEdit).length, 1);
});

scenario("a_failed_save_keeps_the_form_and_what_was_typed_and_applies_nothing", async () => {
  const page = await open();
  page.el("profile-open").click();
  await page.settle();
  await page.settle();
  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  page.editField(page.el("profile-name"), "Новое имя");
  const large = page
    .el("profile-body")
    .find((element) => element.getAttribute("type") === "radio" && element.getAttribute("value") === "large")[0];
  large.checked = true;
  large.fire("change");
  page.env.net.route("PATCH", "/api/profile", fail());

  page.el("profile-body").buttons(Logic.TEXT.profileSave)[0].click();
  await page.settle();
  await page.settle();

  assert.equal(page.el("profile-name").value, "Новое имя", "the typing is kept");
  const radios = page.el("profile-body").find((element) => element.getAttribute("type") === "radio");
  assert.equal(radios.find((radio) => radio.getAttribute("value") === "large").checked, true);
  assert.ok(page.el("profile-body").textContent.includes(Logic.TEXT.network));
  assert.equal(page.el("profile-body").buttons(Logic.TEXT.profileSave).length, 1, "the form is still there");
  assert.equal(page.el("profile-body").buttons(Logic.TEXT.profileEdit).length, 0);
  assert.equal(page.doc.documentElement.getAttribute("data-text-size"), null, "an unsaved size is not applied");
  assert.ok(!page.el("profile-body").textContent.includes(Logic.TEXT.profileSaved));
});

scenario("a_refused_name_stays_under_its_field_and_the_form_stays_open", async () => {
  const page = await open();
  page.el("profile-open").click();
  await page.settle();
  await page.settle();
  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  page.editField(page.el("profile-name"), "Иван");
  page.env.net.route("PATCH", "/api/profile", apiError(400, "invalid_display_name"));

  page.el("profile-body").buttons(Logic.TEXT.profileSave)[0].click();
  await page.settle();
  await page.settle();

  assert.ok(page.el("profile-body").textContent.includes(Logic.CODE_MESSAGES.invalid_display_name));
  assert.equal(page.el("profile-name").getAttribute("aria-invalid"), "true");
  assert.equal(page.el("profile-name").value, "Иван");
  assert.equal(page.el("profile-body").buttons(Logic.TEXT.profileSave).length, 1);
  assert.equal(page.el("profile-body").buttons(Logic.TEXT.profileEdit).length, 0);
  assert.ok(!page.el("profile-body").textContent.includes(Logic.TEXT.profileSaved));
});

scenario("a_revoked_identity_is_drawn_as_revoked_and_no_telegram_subject_is", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route(
      "GET",
      "/api/profile",
      H.profileBody("Елена", "normal", [
        H.loginMethod("telegram", "revoked"),
        H.loginMethod("email", "active", "cook@example.com"),
      ])
    );
  });
  page.el("profile-open").click();
  await page.settle();
  await page.settle();

  const body = page.el("profile-body");
  assert.ok(body.textContent.includes("Telegram"));
  assert.ok(body.textContent.includes(Logic.TEXT.profileLoginEmail));
  assert.ok(body.textContent.includes("cook@example.com"));
  assert.ok(body.textContent.includes(Logic.TEXT.profileStatusRevoked));
  assert.ok(body.textContent.includes(Logic.TEXT.profileStatusActive));
  assert.ok(!body.textContent.includes("1234123412341234123"));
  assert.equal(body.byClass("profile-badge-revoked").length, 1);
  assert.equal(body.byClass("profile-badge-active").length, 1);
});

scenario("a_profile_name_and_address_are_text_not_markup", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route(
      "GET",
      "/api/profile",
      H.profileBody(HOSTILE, "normal", [H.loginMethod("email", "active", HOSTILE)])
    );
  });
  page.el("profile-open").click();
  await page.settle();
  await page.settle();

  assert.ok(page.el("profile-body").textContent.includes(HOSTILE), "the saved name is read as text");
  assert.equal(page.doc.root.byTag("img").length, 0);
  assert.equal(page.doc.root.byTag("svg").length, 0);
  assert.equal(page.doc.root.byTag("script").length, 2, "only the two the server wrote");

  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  assert.equal(page.el("profile-name").value, HOSTILE);
  assert.equal(page.doc.root.byTag("img").length, 0);
  assert.equal(page.doc.root.byTag("svg").length, 0);
});

scenario("a_name_that_was_never_set_is_a_note_and_the_form_opens_empty", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", "/api/profile", H.profileBody(null, "normal"));
  });
  page.el("profile-open").click();
  await page.settle();
  await page.settle();

  const body = page.el("profile-body");
  assert.ok(body.textContent.includes(Logic.TEXT.profileNameNotSet));
  assert.equal(body.byClass("profile-not-set").length, 1);
  assert.ok(!body.textContent.includes("null"));

  body.buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  assert.equal(page.el("profile-name").value, "");
});

// ============================================================================================
// KH-F01-B1: the profile's kitchen measures and the recipe's conversion hints
// ============================================================================================

scenario("the_profile_shows_the_kitchen_measures_with_their_effective_values_and_states", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", "/api/profile", H.profileBody("Елена", "normal", [], { glass_ml: 240 }));
  });
  page.el("profile-open").click();
  await page.settle();
  await page.settle();

  const body = page.el("profile-body");
  assert.ok(body.textContent.includes(Logic.TEXT.profileMeasuresTitle));
  assert.ok(body.textContent.includes(Logic.TEXT.profileMeasureGlass));
  assert.ok(body.textContent.includes(Logic.TEXT.profileMeasureTeaspoon));
  assert.ok(body.textContent.includes(Logic.TEXT.profileMeasureTablespoon));
  // The person's own glass and the two defaults, each said in words.
  assert.ok(body.textContent.includes("240 мл"));
  assert.ok(body.textContent.includes(Logic.TEXT.profileMeasurePersonal));
  assert.ok(body.textContent.includes(Logic.TEXT.profileMeasureDefault));
  assert.ok(body.textContent.includes("5 мл") && body.textContent.includes("15 мл"));
  assert.ok(!body.textContent.includes("250 мл"), "no default value is shown for the overridden glass");
});

scenario("the_profile_form_edits_the_measures_with_a_number_keyboard_hints_and_reset", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", "/api/profile", H.profileBody("Елена", "normal", [], { glass_ml: 240 }));
  });
  page.el("profile-open").click();
  await page.settle();
  await page.settle();
  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();

  const body = page.el("profile-body");
  const glass = page.el("profile-measure-glass_ml");
  assert.equal(glass.value, "240");
  assert.equal(glass.getAttribute("inputmode"), "numeric");
  assert.equal(page.el("profile-measure-teaspoon_ml").value, "");
  assert.equal(page.el("profile-measure-tablespoon_ml").value, "");
  assert.ok(body.textContent.includes("От 50 до 1000 мл"));
  assert.ok(body.textContent.includes("От 1 до 20 мл"));
  assert.ok(body.textContent.includes("От 5 до 50 мл"));
  // One way back to the default per measure, labelled with the measure it belongs to.
  assert.equal(body.buttons(Logic.TEXT.profileMeasureReset).length, 3);
  const reset = body.buttons(Logic.TEXT.profileMeasureReset).find(
    (button) => button.getAttribute("aria-label") === Logic.TEXT.profileMeasureResetLabel.replace("{name}", Logic.TEXT.profileMeasureGlass)
  );
  assert.ok(reset);
  reset.click();
  await page.settle();
  assert.equal(page.el("profile-measure-glass_ml").value, "");
  assert.equal(page.el("profile-measure-teaspoon_ml").value, "", "only its own field is cleared");
});

scenario("saving_a_measure_sends_only_that_measure_and_shows_the_saved_state", async () => {
  const page = await open();
  page.el("profile-open").click();
  await page.settle();
  await page.settle();
  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  page.editField(page.el("profile-measure-glass_ml"), "240");
  page.env.net.route(
    "PATCH",
    "/api/profile",
    H.profileBody("Елена", "normal", [], { glass_ml: 240 })
  );

  page.el("profile-body").buttons(Logic.TEXT.profileSave)[0].click();
  await page.settle();
  await page.settle();

  const requests = page.env.net.requests("PATCH", "/api/profile");
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body, { measures: { glass_ml: 240 } });
  const body = page.el("profile-body");
  assert.ok(body.textContent.includes("240 мл"));
  assert.ok(body.textContent.includes(Logic.TEXT.profileMeasurePersonal));
  assert.equal(body.buttons(Logic.TEXT.profileSave).length, 0, "the form is closed");
  assert.equal(body.buttons(Logic.TEXT.profileEdit).length, 1);
});

scenario("saving_an_emptied_measure_restores_its_default", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", "/api/profile", H.profileBody("Елена", "normal", [], { glass_ml: 240 }));
  });
  page.el("profile-open").click();
  await page.settle();
  await page.settle();
  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  page.editField(page.el("profile-measure-glass_ml"), "");
  page.env.net.route("PATCH", "/api/profile", H.profileBody("Елена", "normal"));

  page.el("profile-body").buttons(Logic.TEXT.profileSave)[0].click();
  await page.settle();
  await page.settle();

  assert.deepEqual(page.env.net.requests("PATCH", "/api/profile")[0].body, {
    measures: { glass_ml: null },
  });
  const body = page.el("profile-body");
  assert.ok(body.textContent.includes("250 мл"));
  assert.ok(body.textContent.includes(Logic.TEXT.profileMeasureDefault));
});

scenario("an_unusable_measure_stays_in_the_form_with_its_worded_error", async () => {
  const page = await open();
  page.el("profile-open").click();
  await page.settle();
  await page.settle();
  page.el("profile-body").buttons(Logic.TEXT.profileEdit)[0].click();
  await page.settle();
  page.editField(page.el("profile-measure-glass_ml"), "49");
  const asked = page.env.net.log.length;

  page.el("profile-body").buttons(Logic.TEXT.profileSave)[0].click();
  await page.settle();
  await page.settle();
  assert.equal(page.env.net.requests("PATCH", "/api/profile").length, 0);
  assert.equal(page.env.net.log.length, asked);
  const body = page.el("profile-body");
  const message = Logic.TEXT.profileMeasureInvalid.replace("{min}", "50").replace("{max}", "1000");
  assert.ok(body.textContent.includes(message));
  const field = page.el("profile-measure-glass_ml");
  assert.equal(field.value, "49", "what was typed is kept");
  assert.equal(field.getAttribute("aria-invalid"), "true");
  assert.equal(page.doc.activeElement, field, "the keyboard goes to the field to fix");
  assert.equal(body.buttons(Logic.TEXT.profileSave).length, 1, "the form stays open");
});

const MEASURED_BORSH_INGREDIENTS = [
  { name: "мука", amount: "1 стакан 200 мл" },
  { name: "масло", amount: "2 ст. л." },
  { name: "сода", amount: "2 ч. л." },
];

function measuredBorshBlock(extra = {}) {
  return {
    target: { glass_ml: 250, teaspoon_ml: 6, tablespoon_ml: 15 },
    source_glass_ml: null,
    source_glass_required: false,
    ingredients: [
      {
        position: 0,
        source: "1 стакан 200 мл",
        source_kind: "household",
        target_unit: "glass",
        status: "nominal_household",
        reason: "converted",
        exact: { numerator: 4, denominator: 5 },
        rounded: null,
        identical: false,
        presentation: "exact",
      },
      {
        position: 1,
        source: "2 ст. л.",
        source_kind: "household",
        target_unit: "tablespoon",
        status: "nominal_household",
        reason: "converted",
        exact: { numerator: 3, denominator: 2 },
        rounded: { numerator: 3, denominator: 2, relative_error: { numerator: 0, denominator: 1 } },
        identical: false,
        presentation: "rounded",
      },
      {
        position: 2,
        source: "2 ч. л.",
        source_kind: "household",
        target_unit: "teaspoon",
        status: "nominal_household",
        reason: "converted",
        exact: { numerator: 5, denominator: 3 },
        rounded: { numerator: 7, denominator: 4, relative_error: { numerator: 1, denominator: 20 } },
        identical: false,
        presentation: "rounded",
      },
    ],
    ...extra,
  };
}

function measuredCard(block, extra = {}) {
  return (env) => {
    env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
    const item = structuredFavoriteItem({
      ingredients: MEASURED_BORSH_INGREDIENTS,
      measures: block,
      ...extra,
    });
    env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
    env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ"));
  };
}

scenario("a_structured_recipe_shows_the_original_amount_and_its_exact_or_rounded_hint", async () => {
  const page = await open(measuredCard(measuredBorshBlock()), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  const body = page.el("recipe-body");
  // The stored amounts are exactly as they were.
  assert.ok(body.textContent.includes("мука — 1 стакан 200 мл"));
  assert.ok(body.textContent.includes("масло — 2 ст. л."));
  assert.ok(body.textContent.includes("сода — 2 ч. л."));
  // The exact value is inline, not hidden; the approximation marker appears only where
  // rounding actually changed the value.
  assert.ok(body.textContent.includes("В ваших мерах"));
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.ok(body.textContent.includes("1½ вашей столовой ложки"));
  assert.ok(!body.textContent.includes("≈ 1½ вашей столовой ложки"));
  assert.ok(body.textContent.includes("≈ 1¾ вашей чайной ложки"));
  // No clarification question: every source glass is declared.
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassQuestion));
});

scenario("an_identity_conversion_is_not_repeated_under_the_ingredient", async () => {
  const block = measuredBorshBlock({
    ingredients: [
      {
        position: 0,
        source: "2 ст. л.",
        source_kind: "household",
        target_unit: "tablespoon",
        status: "nominal_household",
        reason: "converted",
        exact: { numerator: 2, denominator: 1 },
        rounded: { numerator: 2, denominator: 1, relative_error: { numerator: 0, denominator: 1 } },
        identical: true,
        presentation: "none",
      },
    ],
  });
  const page = await open(measuredCard(block), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  const body = page.el("recipe-body");
  assert.ok(body.textContent.includes("масло — 2 ст. л."));
  assert.ok(!body.textContent.includes("В ваших мерах"));
  assert.ok(!body.textContent.includes("2 ваших столовых ложки"));
});

scenario("a_recipe_without_conversion_data_stays_readable", async () => {
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      const item = structuredFavoriteItem({ measures: null });
      env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
      env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ"));
    },
    { structured: true }
  );
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  const body = page.el("recipe-body");
  assert.ok(body.textContent.includes("свекла — 2 шт."));
  assert.ok(body.textContent.includes("Сварите бульон."));
  assert.ok(!body.textContent.includes("В ваших мерах"));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassQuestion));
  assert.equal(body.buttons("Редактировать").length, 1);
});

scenario("the_source_glass_question_offers_200_250_and_another_volume_with_nothing_preselected", async () => {
  const block = measuredBorshBlock({
    source_glass_required: true,
    ingredients: [
      {
        position: 0,
        source: "1 стакан",
        source_kind: "household",
        target_unit: "glass",
        status: "clarification_required",
        reason: "glass_size_required",
        exact: null,
        rounded: null,
        identical: false,
        presentation: "none",
      },
    ],
  });
  const page = await open(measuredCard(block), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  let body = page.el("recipe-body");
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassQuestion));
  const radios = [page.el("source-glass-200"), page.el("source-glass-250")];
  assert.deepEqual(radios.map((radio) => radio.checked), [false, false]);
  // The custom capacity is a disclosure, not a third radio: nothing is selected by
  // opening it, and its field starts empty and closed.
  const toggle = page.el("source-glass-other-toggle");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(toggle.getAttribute("aria-controls"), "source-glass-other-panel");
  assert.equal(page.el("source-glass-other-panel").hidden, true);
  assert.equal(page.el("source-glass-other").value, "");

  // Choosing 200 mL reads the same recipe with the clarification as a query parameter.
  const chosen = structuredFavoriteItem({
    ingredients: MEASURED_BORSH_INGREDIENTS,
    measures: measuredBorshBlock({
      source_glass_ml: 200,
      // The real answer keeps saying that an undeclared glass is in the amount (the
      // clarification is a reading of it, not a replacement of it).
      source_glass_required: true,
      ingredients: [
        {
          position: 0,
          source: "1 стакан",
          source_kind: "household",
          target_unit: "glass",
          status: "nominal_household",
          reason: "converted",
          exact: { numerator: 4, denominator: 5 },
          rounded: null,
          identical: false,
          presentation: "exact",
        },
      ],
    }),
  });
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(chosen, "Борщ"));
  page.el("source-glass-200").checked = true;
  page.el("source-glass-200").fire("change");
  await page.settle();
  await page.settle();

  assert.ok(
    page.env.net.requests("GET", `/api/favorites/${H.FAV_1}`).slice(-1)[0].url.endsWith(
      "?source_glass_ml=200"
    )
  );
  body = page.el("recipe-body");
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.equal(page.el("source-glass-200").checked, true, "the choice is kept while the recipe is open");
});

scenario("another_volume_is_validated_and_only_a_valid_one_is_applied", async () => {
  const block = measuredBorshBlock({
    source_glass_required: true,
    ingredients: [
      {
        position: 0,
        source: "1 стакан",
        source_kind: "household",
        target_unit: "glass",
        status: "clarification_required",
        reason: "glass_size_required",
        exact: null,
        rounded: null,
        identical: false,
        presentation: "none",
      },
    ],
  });
  const page = await open(measuredCard(block), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  const asked = page.env.net.log.length;

  page.el("source-glass-other-toggle").click();
  await page.settle();
  assert.equal(page.el("source-glass-other-toggle").getAttribute("aria-expanded"), "true");
  assert.equal(page.el("source-glass-other-panel").hidden, false);
  page.editField(page.el("source-glass-other"), "49");
  page.el("recipe-body").buttons(Logic.TEXT.sourceGlassApply)[0].click();
  await page.settle();

  assert.equal(page.env.net.log.length, asked, "an unusable volume sends nothing");
  assert.ok(page.el("recipe-body").textContent.includes(Logic.TEXT.sourceGlassInvalid));
  assert.equal(page.el("source-glass-other").getAttribute("aria-invalid"), "true");
  assert.deepEqual(
    [page.el("source-glass-200").checked, page.el("source-glass-250").checked],
    [false, false],
    "an unusable custom volume selects nothing"
  );

  const chosen = structuredFavoriteItem({
    ingredients: MEASURED_BORSH_INGREDIENTS,
    measures: measuredBorshBlock({
      source_glass_ml: 300,
      // The real answer keeps saying that an undeclared glass is in the amount.
      source_glass_required: true,
      ingredients: [
        {
          position: 0,
          source: "1 стакан",
          source_kind: "household",
          target_unit: "glass",
          status: "nominal_household",
          reason: "converted",
          exact: { numerator: 6, denominator: 5 },
          rounded: { numerator: 6, denominator: 5, relative_error: { numerator: 0, denominator: 1 } },
          identical: false,
          presentation: "rounded",
        },
      ],
    }),
  });
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(chosen, "Борщ"));
  page.editField(page.el("source-glass-other"), "300");
  page.el("recipe-body").buttons(Logic.TEXT.sourceGlassApply)[0].click();
  await page.settle();
  await page.settle();

  assert.ok(
    page.env.net.requests("GET", `/api/favorites/${H.FAV_1}`).slice(-1)[0].url.endsWith(
      "?source_glass_ml=300"
    )
  );
  assert.ok(page.el("recipe-body").textContent.includes("1,2 вашего стакана"));
  // Only the server's answer makes it a choice: the field keeps the confirmed value and
  // the disclosure names it, while neither offered radio is selected.
  assert.deepEqual(
    [page.el("source-glass-200").checked, page.el("source-glass-250").checked],
    [false, false]
  );
  assert.ok(
    page.el("recipe-body").textContent.includes(
      Logic.TEXT.sourceGlassConfirmed.replace("{value}", "300")
    )
  );
  assert.equal(page.el("source-glass-other").value, "300");
});

scenario("a_source_glass_choice_is_pending_while_its_read_is_out_and_a_failure_keeps_the_confirmed_one", async () => {
  const block = measuredBorshBlock({
    source_glass_required: true,
    ingredients: [
      {
        position: 0,
        source: "1 стакан",
        source_kind: "household",
        target_unit: "glass",
        status: "clarification_required",
        reason: "glass_size_required",
        exact: null,
        rounded: null,
        identical: false,
        presentation: "none",
      },
    ],
  });
  const page = await open(measuredCard(block), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  const chosen200 = structuredFavoriteItem({
    ingredients: MEASURED_BORSH_INGREDIENTS,
    measures: measuredBorshBlock({
      source_glass_ml: 200,
      source_glass_required: true,
      ingredients: [
        {
          position: 0,
          source: "1 стакан",
          source_kind: "household",
          target_unit: "glass",
          status: "nominal_household",
          reason: "converted",
          exact: { numerator: 4, denominator: 5 },
          rounded: null,
          identical: false,
          presentation: "exact",
        },
      ],
    }),
  });
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(chosen200, "Борщ"));
  page.el("source-glass-200").checked = true;
  page.el("source-glass-200").fire("change");
  await page.settle();
  assert.equal(page.el("source-glass-200").checked, true);
  assert.ok(page.el("recipe-body").textContent.includes("Точно: 0,8 вашего стакана"));

  // The next read is slow: the confirmed choice and its conversion stay, and the new
  // choice is named as pending -- never selected beside the older conversion.
  const answer = deferred();
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, () => answer.promise);
  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await flush();

  let body = page.el("recipe-body");
  assert.equal(page.el("source-glass-200").checked, true, "the confirmed choice stays selected");
  assert.equal(page.el("source-glass-250").checked, false, "the pending one is not shown as selected");
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"), "its conversion stays");
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassPending.replace("{value}", "250")));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassFailed));

  // The read fails: the confirmed choice and conversion stay, and the failure is worded
  // with a way to ask again.
  answer.resolve(fail());
  await page.settle();
  await page.settle();

  body = page.el("recipe-body");
  assert.equal(page.el("source-glass-200").checked, true);
  assert.equal(page.el("source-glass-250").checked, false);
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassFailed));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassPending.replace("{value}", "250")));
  assert.equal(body.buttons(Logic.TEXT.retry).length, 1);

  // The retry succeeds for 250: its block is drawn with it and no older value remains.
  const chosen250 = structuredFavoriteItem({
    ingredients: MEASURED_BORSH_INGREDIENTS,
    measures: measuredBorshBlock({
      source_glass_ml: 250,
      source_glass_required: true,
      ingredients: [
        {
          position: 0,
          source: "1 стакан",
          source_kind: "household",
          target_unit: "glass",
          status: "nominal_household",
          reason: "converted",
          exact: { numerator: 1, denominator: 1 },
          rounded: null,
          identical: true,
          presentation: "none",
        },
      ],
    }),
  });
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(chosen250, "Борщ"));
  page.el("recipe-body").buttons(Logic.TEXT.retry)[0].click();
  await page.settle();
  await page.settle();

  body = page.el("recipe-body");
  assert.equal(page.el("source-glass-250").checked, true);
  assert.equal(page.el("source-glass-200").checked, false);
  assert.ok(!body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassFailed));
});

// --- KH-F01-B1 remediation 02: the custom capacity is a draft, not a choice ----------------

// One glass ingredient converted from a confirmed source capacity, in the shape of the
// API's block: the numbers only have to be the ones the page draws for that context.
function sourceGlassBlock(glass, exact = { numerator: 4, denominator: 5 }) {
  return measuredBorshBlock({
    source_glass_ml: glass,
    source_glass_required: true,
    ingredients: [
      {
        position: 0,
        source: "1 стакан",
        source_kind: "household",
        target_unit: "glass",
        status: "nominal_household",
        reason: "converted",
        exact,
        rounded: null,
        identical: false,
        presentation: "exact",
      },
    ],
  });
}

function clarificationBlock() {
  return measuredBorshBlock({
    source_glass_required: true,
    ingredients: [
      {
        position: 0,
        source: "1 стакан",
        source_kind: "household",
        target_unit: "glass",
        status: "clarification_required",
        reason: "glass_size_required",
        exact: null,
        rounded: null,
        identical: false,
        presentation: "none",
      },
    ],
  });
}

async function openMeasuredCard(block, extra = {}) {
  const page = await open(measuredCard(block, extra), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  return page;
}

async function confirmSourceGlass(page, glass, exact) {
  const item = structuredFavoriteItem({
    ingredients: MEASURED_BORSH_INGREDIENTS,
    measures: sourceGlassBlock(glass, exact),
  });
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ"));
  page.el(`source-glass-${glass}`).checked = true;
  page.el(`source-glass-${glass}`).fire("change");
  await page.settle();
  await page.settle();
}

// The node with this id inside the card as it is drawn now (the document registry also
// remembers nodes of earlier drawings, so the current card's own subtree is searched).
function cardNode(page, id) {
  return page.el("recipe-body").find((node) => node.id === id)[0] || null;
}

scenario("a_custom_volume_is_a_draft_and_never_unchecks_the_confirmed_choice", async () => {
  const page = await openMeasuredCard(clarificationBlock());
  await confirmSourceGlass(page, 200, { numerator: 4, denominator: 5 });

  let body = page.el("recipe-body");
  assert.equal(page.el("source-glass-200").checked, true);
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  const asked = page.env.net.log.length;

  page.el("source-glass-other-toggle").click();
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(page.el("source-glass-other-toggle").getAttribute("aria-expanded"), "true");
  assert.equal(page.el("source-glass-other-panel").hidden, false);
  assert.equal(page.doc.activeElement, page.el("source-glass-other"), "the opened field takes the keyboard");
  assert.equal(page.env.net.log.length, asked, "opening a disclosure sends nothing");

  // Typing a capacity is the person's draft: no selection changes, no conversion changes.
  page.editField(page.el("source-glass-other"), "300");
  body = page.el("recipe-body");
  assert.equal(page.el("source-glass-200").checked, true, "the confirmed choice stays selected");
  assert.equal(page.el("source-glass-250").checked, false);
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"), "its conversion stays");
  const draftHint = page.el("source-glass-draft-hint");
  assert.equal(draftHint.hidden, false, "the field says what it is: a draft");
  assert.ok(draftHint.textContent.includes(Logic.TEXT.sourceGlassDraftHint));
  assert.equal(page.env.net.log.length, asked, "typing sends nothing");

  // An unusable volume is refused in the field and changes no choice either.
  page.editField(page.el("source-glass-other"), "49");
  page.el("recipe-body").buttons(Logic.TEXT.sourceGlassApply)[0].click();
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(page.env.net.log.length, asked, "an unusable volume sends nothing");
  assert.equal(page.el("source-glass-200").checked, true);
  assert.equal(page.el("source-glass-250").checked, false);
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassInvalid));
  assert.equal(page.el("source-glass-other").getAttribute("aria-invalid"), "true");
  assert.equal(page.el("source-glass-draft-hint").hidden, false);
});

scenario("a_custom_volume_becomes_a_choice_only_with_its_own_answer_and_is_shown_when_reopened", async () => {
  const page = await openMeasuredCard(clarificationBlock());
  await confirmSourceGlass(page, 200, { numerator: 4, denominator: 5 });
  page.el("source-glass-other-toggle").click();
  await page.settle();
  page.editField(page.el("source-glass-other"), "300");

  const answer = deferred();
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, () => answer.promise);
  page.el("recipe-body").buttons(Logic.TEXT.sourceGlassApply)[0].click();
  await flush();

  // The read is out: the confirmed 200 and its conversion stay, and the new value is only
  // named as pending.
  let body = page.el("recipe-body");
  assert.equal(page.el("source-glass-200").checked, true);
  assert.equal(page.el("source-glass-250").checked, false);
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassPending.replace("{value}", "300")));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassConfirmed.replace("{value}", "300")));
  assert.equal(page.el("source-glass-other-panel").getAttribute("aria-busy"), "true");
  assert.equal(page.el("source-glass-other").value, "300", "the draft stays while it is applied");

  answer.resolve(
    H.favoriteDetail(
      structuredFavoriteItem({
        ingredients: MEASURED_BORSH_INGREDIENTS,
        measures: sourceGlassBlock(300, { numerator: 6, denominator: 5 }),
      }),
      "Борщ"
    )
  );
  await page.settle();
  await page.settle();

  body = page.el("recipe-body");
  assert.ok(
    page.env.net.requests("GET", `/api/favorites/${H.FAV_1}`).slice(-1)[0].url.endsWith(
      "?source_glass_ml=300"
    )
  );
  assert.equal(page.el("source-glass-200").checked, false);
  assert.equal(page.el("source-glass-250").checked, false);
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassConfirmed.replace("{value}", "300")));
  assert.ok(body.textContent.includes("1,2 вашего стакана"));
  assert.equal(page.el("source-glass-other-panel").hidden, false);
  assert.equal(page.el("source-glass-other").value, "300");
  assert.equal(page.el("source-glass-draft-hint").hidden, true, "the field holds the confirmed value");

  // Closing the disclosure leaves the confirmed capacity and its conversion on the card.
  page.el("source-glass-other-toggle").click();
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(page.el("source-glass-other-toggle").getAttribute("aria-expanded"), "false");
  assert.equal(page.el("source-glass-other-panel").hidden, true);
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassConfirmed.replace("{value}", "300")));
  assert.ok(body.textContent.includes("1,2 вашего стакана"));

  // Reopening shows that very capacity in the field, not an empty one.
  page.el("source-glass-other-toggle").click();
  await page.settle();
  assert.equal(page.el("source-glass-other-toggle").getAttribute("aria-expanded"), "true");
  assert.equal(page.el("source-glass-other").value, "300");
  assert.equal(page.el("source-glass-draft-hint").hidden, true);
  assert.equal(page.doc.activeElement, page.el("source-glass-other"));
});

scenario("a_failed_custom_volume_keeps_the_confirmed_choice_and_the_retry_uses_the_same_value", async () => {
  const page = await openMeasuredCard(clarificationBlock());
  await confirmSourceGlass(page, 200, { numerator: 4, denominator: 5 });
  page.el("source-glass-other-toggle").click();
  await page.settle();

  page.editField(page.el("source-glass-other"), "300");
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, fail());
  page.el("recipe-body").buttons(Logic.TEXT.sourceGlassApply)[0].click();
  await page.settle();
  await page.settle();

  let body = page.el("recipe-body");
  assert.equal(page.el("source-glass-200").checked, true, "the confirmed choice is untouched");
  assert.equal(page.el("source-glass-250").checked, false);
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"), "its conversion is untouched");
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassFailed));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassPending.replace("{value}", "300")));
  assert.equal(page.el("source-glass-other").value, "300", "the typed value is kept for the retry");
  assert.equal(page.el("source-glass-other-panel").getAttribute("aria-busy"), "false");

  // The retry asks for the same value, and only its own answer confirms it.
  page.env.net.route(
    "GET",
    `/api/favorites/${H.FAV_1}`,
    H.favoriteDetail(
      structuredFavoriteItem({
        ingredients: MEASURED_BORSH_INGREDIENTS,
        measures: sourceGlassBlock(300, { numerator: 6, denominator: 5 }),
      }),
      "Борщ"
    )
  );
  page.el("recipe-body").buttons(Logic.TEXT.retry)[0].click();
  await page.settle();
  await page.settle();

  body = page.el("recipe-body");
  assert.ok(
    page.env.net.requests("GET", `/api/favorites/${H.FAV_1}`).slice(-1)[0].url.endsWith(
      "?source_glass_ml=300"
    )
  );
  assert.equal(page.el("source-glass-200").checked, false);
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassConfirmed.replace("{value}", "300")));
  assert.ok(body.textContent.includes("1,2 вашего стакана"));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassFailed));
  assert.equal(page.el("source-glass-other").value, "300");
});

scenario("only_the_newest_source_glass_answer_changes_the_card_whatever_order_answers_arrive", async () => {
  const page = await openMeasuredCard(clarificationBlock());
  await confirmSourceGlass(page, 200, { numerator: 4, denominator: 5 });

  const older = deferred();
  const newer = deferred();
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, () => older.promise, () => newer.promise);

  // 250 mL is asked for; while its read is out the person opens the custom field and asks
  // for 300 mL. Only the newest answer may become the card.
  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await flush();
  assert.ok(
    page.el("recipe-body").textContent.includes(Logic.TEXT.sourceGlassPending.replace("{value}", "250"))
  );

  page.el("source-glass-other-toggle").click();
  await page.settle();
  page.editField(page.el("source-glass-other"), "300");
  page.el("recipe-body").buttons(Logic.TEXT.sourceGlassApply)[0].click();
  await flush();

  let body = page.el("recipe-body");
  assert.equal(page.el("source-glass-200").checked, true);
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassPending.replace("{value}", "300")));

  // The older answer (250) arrives first: it must not replace the 200 mL card even for a
  // moment, and the newer request stays the pending one.
  older.resolve(
    H.favoriteDetail(
      structuredFavoriteItem({
        ingredients: MEASURED_BORSH_INGREDIENTS,
        measures: sourceGlassBlock(250, { numerator: 1, denominator: 1 }),
      }),
      "Борщ"
    )
  );
  await flush();
  body = page.el("recipe-body");
  assert.equal(page.el("source-glass-200").checked, true);
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassPending.replace("{value}", "300")));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassConfirmed.replace("{value}", "250")));

  newer.resolve(
    H.favoriteDetail(
      structuredFavoriteItem({
        ingredients: MEASURED_BORSH_INGREDIENTS,
        measures: sourceGlassBlock(300, { numerator: 6, denominator: 5 }),
      }),
      "Борщ"
    )
  );
  await page.settle();
  await page.settle();

  body = page.el("recipe-body");
  assert.equal(page.el("source-glass-200").checked, false);
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassConfirmed.replace("{value}", "300")));
  assert.ok(body.textContent.includes("1,2 вашего стакана"));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassPending.replace("{value}", "300")));
});

scenario("switching_recipes_while_a_custom_volume_is_pending_changes_nothing_here", async () => {
  const first = structuredFavoriteItem({
    ingredients: MEASURED_BORSH_INGREDIENTS,
    measures: clarificationBlock(),
  });
  const second = H.structuredFavorite(H.FAV_2, 2, "Уха", 1, 0, {
    servings: null,
    ingredients: [{ name: "рыба", amount: "1 кг" }],
    steps: ["Сварите."],
    version: 1,
    measures: measuredBorshBlock({ source_glass_required: false, ingredients: [] }),
  });
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([first, second]));
      env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(first, "Борщ"));
      env.net.route("GET", `/api/favorites/${H.FAV_2}`, H.favoriteDetail(second, "Уха"));
    },
    { structured: true }
  );
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  await confirmSourceGlass(page, 200, { numerator: 4, denominator: 5 });
  page.el("source-glass-other-toggle").click();
  await page.settle();
  page.editField(page.el("source-glass-other"), "300");
  const answer = deferred();
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, () => answer.promise);
  page.el("recipe-body").buttons(Logic.TEXT.sourceGlassApply)[0].click();
  await flush();
  assert.ok(
    page.el("recipe-body").textContent.includes(Logic.TEXT.sourceGlassPending.replace("{value}", "300"))
  );

  // Another recipe is opened while the read is out. It starts without any of the first
  // recipe's clarification or draft.
  page.el("favorite-list").byTag("button").find((button) => button.textContent.includes("Уха")).click();
  await page.settle();
  assert.equal(page.el("recipe-title").textContent, "Уха");
  assert.equal(cardNode(page, "source-glass-other-toggle"), null, "the other recipe asks no question");
  assert.equal(cardNode(page, "source-glass-other"), null);
  assert.ok(!page.el("recipe-body").textContent.includes(Logic.TEXT.sourceGlassQuestion));

  // The answer to the abandoned request cannot reach the card that is open now.
  answer.resolve(
    H.favoriteDetail(
      structuredFavoriteItem({
        ingredients: MEASURED_BORSH_INGREDIENTS,
        measures: sourceGlassBlock(300, { numerator: 6, denominator: 5 }),
      }),
      "Борщ"
    )
  );
  await flush();
  assert.equal(page.el("recipe-title").textContent, "Уха");
  assert.equal(cardNode(page, "source-glass-other-toggle"), null);
  assert.ok(
    !page.el("recipe-body").textContent.includes(Logic.TEXT.sourceGlassConfirmed.replace("{value}", "300"))
  );

  // Returning to the first recipe is a fresh start for its clarification too: the question
  // is asked again with nothing confirmed, an empty field and the disclosure closed.
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(first, "Борщ"));
  page.el("favorite-list").byTag("button").find((button) => button.textContent.includes("Борщ")).click();
  await page.settle();
  assert.equal(page.el("recipe-title").textContent, "Борщ");
  const toggle = cardNode(page, "source-glass-other-toggle");
  assert.ok(toggle, "the question is asked for the recipe that is open");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(cardNode(page, "source-glass-other-panel").hidden, true);
  assert.equal(cardNode(page, "source-glass-other").value, "");
  assert.equal(cardNode(page, "source-glass-200").checked, false);
  assert.equal(cardNode(page, "source-glass-250").checked, false);
  assert.ok(
    !page.el("recipe-body").textContent.includes(Logic.TEXT.sourceGlassConfirmed.replace("{value}", "300"))
  );
});

scenario("the_custom_volume_disclosure_is_labelled_and_keyboard_operable_at_any_width", async () => {
  for (const narrow of [false, true]) {
    const page = await open(measuredCard(clarificationBlock()), { structured: true, narrow });
    page.el("tab-favorites").click();
    await page.settle();
    page.el("favorite-list").byTag("button")[0].click();
    await page.settle();

    const why = "narrow=" + narrow;
    let toggle = page.el("source-glass-other-toggle");
    assert.equal(toggle.localName, "button", why);
    assert.equal(toggle.getAttribute("type"), "button", why);
    assert.equal(toggle.getAttribute("aria-expanded"), "false", why);
    assert.equal(toggle.getAttribute("aria-controls"), "source-glass-other-panel", why);
    assert.equal(page.el("source-glass-other-panel").hidden, true, why);

    // The field has a real label and points at its own explanation and error.
    const labels = page
      .el("recipe-body")
      .byTag("label")
      .filter((label) => label.getAttribute("for") === "source-glass-other");
    assert.equal(labels.length, 1, why);
    assert.ok(labels[0].textContent.includes(Logic.TEXT.sourceGlassOtherLabel), why);
    const described = page.el("source-glass-other").getAttribute("aria-describedby");
    assert.ok(described.includes("source-glass-draft-hint"), why);
    assert.ok(described.includes("source-glass-error"), why);

    // A button is activated by Enter and Space in the browser: the same click path.
    toggle.click();
    await page.settle();
    toggle = page.el("source-glass-other-toggle");
    assert.equal(toggle.getAttribute("aria-expanded"), "true", why);
    assert.equal(page.el("source-glass-other-panel").hidden, false, why);
    assert.equal(page.doc.activeElement, page.el("source-glass-other"), why);

    toggle.click();
    await page.settle();
    toggle = page.el("source-glass-other-toggle");
    assert.equal(toggle.getAttribute("aria-expanded"), "false", why);
    assert.equal(page.el("source-glass-other-panel").hidden, true, why);
    assert.equal(page.doc.activeElement, toggle, why);
  }
});

// --- KH-F01-B1 remediation 02: an editor keeps its own baseline version ---------------------

scenario("an_editor_opened_while_a_source_glass_read_is_out_keeps_and_saves_its_own_version", async () => {
  const page = await openMeasuredCard(clarificationBlock());
  await confirmSourceGlass(page, 200, { numerator: 4, denominator: 5 });

  const pending = deferred();
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, () => pending.promise);
  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await flush();

  // The editor is opened while the 250 mL read is still out: its baseline is version 3.
  page.el("recipe-body").buttons(Logic.TEXT.editAction)[0].click();
  await page.settle();
  assert.equal(page.el("recipe-body").byClass("edit-title")[0].value, "Борщ");
  assert.deepEqual(
    page.el("recipe-body").byClass("ingredient-name").map((input) => input.value),
    MEASURED_BORSH_INGREDIENTS.map((ingredient) => ingredient.name)
  );

  // The answer that was out carries version 4, made on another device. It must not
  // replace the recipe the draft was opened on.
  pending.resolve(
    H.favoriteDetail(
      structuredFavoriteItem({
        title: "Борщ, изменённый в другом месте",
        ingredients: [{ name: "свекла", amount: "9 шт." }],
        version: 4,
        measures: sourceGlassBlock(250, { numerator: 1, denominator: 1 }),
      }),
      "Борщ, изменённый в другом месте"
    )
  );
  await page.settle();
  await page.settle();
  assert.equal(
    page.el("recipe-body").byClass("edit-title")[0].value,
    "Борщ",
    "the version under the draft did not move"
  );
  assert.deepEqual(
    page.el("recipe-body").byClass("ingredient-name").map((input) => input.value),
    MEASURED_BORSH_INGREDIENTS.map((ingredient) => ingredient.name),
    "the draft contents are exactly as they were opened"
  );
  assert.deepEqual(
    page.el("recipe-body").byClass("ingredient-amount").map((input) => input.value),
    MEASURED_BORSH_INGREDIENTS.map((ingredient) => ingredient.amount)
  );

  // The draft is saved with the version it was opened on. The server refuses it as a
  // conflict; the draft stays and nothing is overwritten.
  page.editField(page.el("recipe-body").byClass("edit-title")[0], "Мой борщ");
  page.env.net.route("PATCH", `/api/favorites/${H.FAV_1}`, apiError(409, "favorite_conflict"));
  page.el("recipe-body").buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();
  await page.settle();

  const sent = page.env.net.requests("PATCH", `/api/favorites/${H.FAV_1}`)[0].body;
  assert.equal(sent.expected_version, 3, "the stale draft never names version 4");
  assert.equal(sent.title, "Мой борщ");
  assert.equal(page.el("recipe-body").byClass("edit-title")[0].value, "Мой борщ");
  assert.ok(page.el("recipe-body").textContent.includes(Logic.TEXT.editConflict));

  // "Загрузить заново" shows what is stored now: the other device's version, intact.
  const changed = structuredFavoriteItem({
    title: "Борщ, изменённый в другом месте",
    ingredients: [{ name: "свекла", amount: "9 шт." }],
    version: 4,
    measures: sourceGlassBlock(200, { numerator: 4, denominator: 5 }),
  });
  page.env.net.route(
    "GET",
    `/api/favorites/${H.FAV_1}`,
    H.favoriteDetail(changed, "Борщ, изменённый в другом месте")
  );
  page.el("recipe-body").buttons(Logic.TEXT.editReload)[0].click();
  await page.settle();
  await page.settle();

  assert.equal(page.el("recipe-title").textContent, "Борщ, изменённый в другом месте");
  assert.ok(page.el("recipe-body").textContent.includes("свекла — 9 шт."));
  assert.equal(page.el("recipe-body").buttons(Logic.TEXT.editSave).length, 0, "the editor is closed");
  assert.ok(
    page.el("recipe-body").textContent.includes("Точно: 0,8 вашего стакана"),
    "the confirmed 200 mL choice belongs to the reloaded version"
  );
});

scenario("a_saved_edit_whose_conversion_read_fails_shows_the_saved_recipe_without_a_stale_hint", async () => {
  const block = measuredBorshBlock({
    source_glass_required: true,
    ingredients: [
      {
        position: 0,
        source: "1 стакан",
        source_kind: "household",
        target_unit: "glass",
        status: "clarification_required",
        reason: "glass_size_required",
        exact: null,
        rounded: null,
        identical: false,
        presentation: "none",
      },
    ],
  });
  const page = await open(measuredCard(block), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  const chosen200 = structuredFavoriteItem({
    ingredients: MEASURED_BORSH_INGREDIENTS,
    measures: measuredBorshBlock({
      source_glass_ml: 200,
      source_glass_required: true,
      ingredients: [
        {
          position: 0,
          source: "1 стакан",
          source_kind: "household",
          target_unit: "glass",
          status: "nominal_household",
          reason: "converted",
          exact: { numerator: 4, denominator: 5 },
          rounded: null,
          identical: false,
          presentation: "exact",
        },
      ],
    }),
  });
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(chosen200, "Борщ"));
  page.el("source-glass-200").checked = true;
  page.el("source-glass-200").fire("change");
  await page.settle();
  assert.ok(page.el("recipe-body").textContent.includes("Точно: 0,8 вашего стакана"));

  page.el("recipe-body").buttons(Logic.TEXT.editAction)[0].click();
  await page.settle();
  page.el("recipe-body").byClass("edit-title")[0].value = "Борщ по-домашнему";
  const updated = structuredFavoriteItem({
    title: "Борщ по-домашнему",
    ingredients: MEASURED_BORSH_INGREDIENTS,
    version: 4,
    measures: measuredBorshBlock(), // the edit's own block, without the clarification
  });
  page.env.net.route(
    "PATCH",
    `/api/favorites/${H.FAV_1}`,
    H.favoriteDetail(updated, "Борщ по-домашнему")
  );
  page.env.net.route("GET", `/api/favorites/${H.FAV_1}`, fail());
  page.el("recipe-body").buttons(Logic.TEXT.editSave)[0].click();
  await page.settle();
  await page.settle();

  // The edit is on the card, but no conversion is drawn from the unconfirmed block.
  let body = page.el("recipe-body");
  assert.equal(page.el("recipe-title").textContent, "Борщ по-домашнему");
  assert.ok(body.textContent.includes("мука — 1 стакан 200 мл"));
  assert.ok(!body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.ok(body.textContent.includes(Logic.TEXT.measuresRefreshFailed));
  assert.equal(body.buttons(Logic.TEXT.retry).length, 1);

  // "Повторить" reads the card again with the confirmed choice.
  const refreshed = structuredFavoriteItem({
    title: "Борщ по-домашнему",
    ingredients: MEASURED_BORSH_INGREDIENTS,
    version: 4,
    measures: chosen200.measures,
  });
  page.env.net.route(
    "GET",
    `/api/favorites/${H.FAV_1}`,
    H.favoriteDetail(refreshed, "Борщ по-домашнему")
  );
  page.el("recipe-body").buttons(Logic.TEXT.retry)[0].click();
  await page.settle();
  await page.settle();

  body = page.el("recipe-body");
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.ok(!body.textContent.includes(Logic.TEXT.measuresRefreshFailed));
  assert.ok(
    page.env.net.requests("GET", `/api/favorites/${H.FAV_1}`).slice(-1)[0].url.endsWith(
      "?source_glass_ml=200"
    )
  );
});

scenario("a_conversion_that_contradicts_its_own_status_draws_no_number_and_the_recipe_stays", async () => {
  const block = measuredBorshBlock({
    source_glass_required: true,
    ingredients: [
      {
        position: 0,
        source: "1 стакан",
        source_kind: "household",
        target_unit: "glass",
        status: "unsupported",
        reason: "source_not_quantified",
        exact: { numerator: 99, denominator: 1 },
        rounded: null,
        identical: false,
        presentation: "exact",
      },
    ],
  });
  const page = await open(measuredCard(block), { structured: true });
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();

  const body = page.el("recipe-body");
  assert.ok(body.textContent.includes("мука — 1 стакан 200 мл"));
  assert.ok(!body.textContent.includes("Точно: 99"));
  assert.ok(!body.textContent.includes("В ваших мерах"));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassQuestion));
});

// KH-F01-B1.1-D: step hints, the safe «Не знаю» answer and the approved copy
// ============================================================================================

// The card of a structured favorite whose steps carry the block's annotations.
function stepsCard(block, steps, extra = {}) {
  return (env) => {
    env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
    const item = structuredFavoriteItem({ steps, measures: block, ...extra });
    env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
    env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ"));
  };
}

async function openCard(page) {
  page.el("tab-favorites").click();
  await page.settle();
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
}

// The text of a node without any of its elements: what a reader may copy from the step.
const textOnly = (node) =>
  node.childNodes
    .filter((child) => child.nodeType === 3)
    .map((child) => child.data)
    .join("");

const stepItems = (body) => body.byClass("recipe-steps")[0].byTag("li");

scenario("a_step_hint_is_a_separate_element_after_its_amount_and_the_text_is_unchanged", async () => {
  const step = "Доведите до кипения, добавьте 500 мл воды.";
  const page = await open(
    stepsCard(H.stepBlock(3, [H.stepAnnotation(0, step, [H.span(step, "500 мл")])]), [step]),
    { structured: true }
  );
  await openCard(page);

  const body = page.el("recipe-body");
  const [item] = stepItems(body);
  assert.equal(textOnly(item), step, "the text nodes are the stored step, untouched");
  assert.equal(item.textContent, step.slice(0, step.indexOf("500 мл")) + "500 мл (2 ваших стакана)" + step.slice(step.indexOf("500 мл") + "500 мл".length));
  const hints = item.byClass("measure-hint");
  assert.equal(hints.length, 1);
  assert.equal(hints[0].textContent, " (2 ваших стакана)");
  // Immediately after the amount it explains, not at the end of the step.
  const children = item.childNodes;
  const at = children.indexOf(hints[0]);
  assert.equal(children[at - 1].data, "500 мл");
  assert.equal(children[at + 1].data, " воды.");
});

scenario("two_step_amounts_are_hinted_independently_where_each_belongs", async () => {
  const step = "Влейте 500 мл воды и добавьте 250 мл молока.";
  const exactOne = {
    target_unit: "glass",
    status: "nominal_household",
    reason: "converted",
    exact: { numerator: 1, denominator: 1 },
    rounded: { numerator: 1, denominator: 1, relative_error: { numerator: 0, denominator: 1 } },
    identical: false,
    presentation: "rounded",
  };
  const page = await open(
    stepsCard(
      H.stepBlock(3, [
        H.stepAnnotation(0, step, [
          H.span(step, "500 мл"),
          H.span(step, "250 мл", { conversion: exactOne }),
        ]),
      ]),
      [step]
    ),
    { structured: true }
  );
  await openCard(page);

  const [item] = stepItems(page.el("recipe-body"));
  assert.equal(textOnly(item), step);
  assert.equal(item.byClass("measure-hint").length, 2);
  assert.ok(item.textContent.includes("500 мл (2 ваших стакана)"));
  assert.ok(item.textContent.includes("250 мл (1 ваш стакан)"));
});

scenario("an_emoji_before_an_amount_does_not_shift_its_hint", async () => {
  const step = "Добавьте 🥛 500 мл воды 🥛.";
  const page = await open(
    stepsCard(H.stepBlock(3, [H.stepAnnotation(0, step, [H.span(step, "500 мл")])]), [step]),
    { structured: true }
  );
  await openCard(page);

  const [item] = stepItems(page.el("recipe-body"));
  assert.equal(textOnly(item), step, "the astral characters are the stored ones");
  const hints = item.byClass("measure-hint");
  assert.equal(hints.length, 1);
  const at = item.childNodes.indexOf(hints[0]);
  assert.equal(item.childNodes[at - 1].data, "500 мл");
  assert.ok(item.textContent.includes("🥛 500 мл (2 ваших стакана)"));
});

scenario("a_combining_mark_inside_the_amount_keeps_the_span_exact", async () => {
  const step = "Добавьте 500 мл́ воды.";
  const piece = "500 мл́";
  const page = await open(
    stepsCard(H.stepBlock(3, [H.stepAnnotation(0, step, [H.span(step, piece)])]), [step]),
    { structured: true }
  );
  await openCard(page);

  const [item] = stepItems(page.el("recipe-body"));
  assert.equal(textOnly(item), step);
  assert.equal(item.byClass("measure-hint").length, 1);
  assert.ok(item.textContent.includes(piece + " (2 ваших стакана)"));
});

scenario("an_annotation_that_does_not_match_its_step_is_not_drawn_at_all", async () => {
  const step = "Добавьте 500 мл воды.";
  const good = H.span(step, "500 мл");
  const broken = [
    // Overlapping spans.
    [good, { ...H.span(step, "500 мл воды"), start: good.start + 1, end: good.end + 4 }],
    // The slice is not the text the span claims.
    [{ ...good, text: "600 мл" }],
    // The span runs past the stored step.
    [{ ...good, start: 999, end: 1005 }],
  ];
  for (const spans of broken) {
    const page = await open(
      stepsCard(H.stepBlock(3, [H.stepAnnotation(0, step, spans)]), [step]),
      { structured: true }
    );
    await openCard(page);
    const [item] = stepItems(page.el("recipe-body"));
    assert.equal(textOnly(item), step);
    assert.equal(item.byClass("measure-hint").length, 0, JSON.stringify(spans));
    assert.equal(item.textContent, step);
  }
});

scenario("no_step_hint_is_drawn_for_an_amount_the_server_could_not_convert", async () => {
  const step = "Добавьте 500 мл воды и 200 г муки.";
  const noConversion = (extra) =>
    H.span(step, extra.piece || "500 мл", { conversion: null, ...extra });
  const cases = [
    { link: "unresolved", link_reason: "no_local_binding", capacity_origin: "unresolved" },
    { link: "ambiguous", link_reason: "multiple_candidates", capacity_origin: "declared_text" },
    { link: "shared", link_reason: "shared_amount", capacity_origin: "not_applicable" },
    { link: "unsupported", link_reason: "range", capacity_origin: "not_applicable" },
    { link: "linked", link_reason: "name_after", capacity_origin: "not_applicable", conversion_omitted_reason: "mass", piece: "200 г" },
  ];
  for (const extra of cases) {
    const piece = extra.piece || "500 мл";
    const page = await open(
      stepsCard(H.stepBlock(3, [H.stepAnnotation(0, step, [noConversion({ ...extra, piece })])]), [step]),
      { structured: true }
    );
    await openCard(page);
    const [item] = stepItems(page.el("recipe-body"));
    assert.equal(item.byClass("measure-hint").length, 0, JSON.stringify(extra));
    assert.equal(item.textContent, step);
  }
});

scenario("exact_rounded_and_the_three_source_words_read_as_the_spec_says", async () => {
  const step = "Возьмите 500 мл воды.";
  const exactPresentation = {
    target_unit: "glass",
    status: "nominal_household",
    reason: "converted",
    exact: { numerator: 4, denominator: 5 },
    rounded: null,
    identical: false,
    presentation: "exact",
  };
  const roundedTwoAndAHalf = {
    target_unit: "glass",
    status: "nominal_household",
    reason: "converted",
    exact: { numerator: 23, denominator: 9 },
    rounded: { numerator: 5, denominator: 2, relative_error: { numerator: 1, denominator: 50 } },
    identical: false,
    presentation: "rounded",
  };
  for (const word of ["около", "примерно", "приблизительно"]) {
    const page = await open(
      stepsCard(
        H.stepBlock(3, [
          H.stepAnnotation(0, step, [
            H.span(step, "500 мл", {
              conversion: roundedTwoAndAHalf,
              source_approximation: { approximate: true, word },
            }),
          ]),
        ]),
        [step]
      ),
      { structured: true }
    );
    await openCard(page);
    const [item] = stepItems(page.el("recipe-body"));
    // The word makes the hint approximate even though two and a half is exact arithmetic.
    assert.ok(item.textContent.includes("≈ 2½ вашего стакана"), word);
    assert.ok(!item.textContent.includes("Точно"), word);
  }

  const exactPage = await open(
    stepsCard(
      H.stepBlock(3, [
        H.stepAnnotation(0, step, [H.span(step, "500 мл", { conversion: exactPresentation })]),
      ]),
      [step]
    ),
    { structured: true }
  );
  await openCard(exactPage);
  assert.ok(
    stepItems(exactPage.el("recipe-body"))[0].textContent.includes("Точно: 0,8 вашего стакана")
  );

  // A rounded value whose error is zero needs no marker at all.
  const zeroError = {
    ...roundedTwoAndAHalf,
    rounded: { numerator: 2, denominator: 1, relative_error: { numerator: 0, denominator: 1 } },
  };
  const plainPage = await open(
    stepsCard(
      H.stepBlock(3, [H.stepAnnotation(0, step, [H.span(step, "500 мл", { conversion: zeroError })])]),
      [step]
    ),
    { structured: true }
  );
  await openCard(plainPage);
  const plain = stepItems(plainPage.el("recipe-body"))[0];
  assert.ok(plain.textContent.includes("500 мл (2 ваших стакана)"));
  assert.ok(!plain.textContent.includes("≈"));
});

scenario("a_step_only_undeclared_glass_asks_the_question_once_with_the_safe_answer_offered", async () => {
  const step = "Добавьте 1 стакан воды.";
  const block = H.stepBlock(
    3,
    [
      H.stepAnnotation(0, step, [
        H.span(step, "1 стакан", {
          capacity_origin: "unresolved",
          conversion: null,
          conversion_omitted_reason: null,
        }),
      ]),
    ],
    { steps_source_glass_required: true, source_glass_required: false }
  );
  const item = structuredFavoriteItem({ steps: [step], measures: block });
  const clarified = structuredFavoriteItem({
    steps: [step],
    measures: H.stepBlock(
      3,
      [H.stepAnnotation(0, step, [H.span(step, "1 стакан", { capacity_origin: "temporary_clarification" })])],
      { steps_source_glass_required: true, source_glass_ml: 250 }
    ),
  });
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(item, "Борщ"),
        H.favoriteDetail(clarified, "Борщ")
      );
    },
    { structured: true }
  );
  await openCard(page);

  const body = page.el("recipe-body");
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassQuestion));
  assert.ok(body.textContent.includes(Logic.TEXT.sourceGlassExplanation));
  assert.equal(body.byClass("measure-clarify").length, 1, "one question per recipe");
  assert.equal(body.buttons(Logic.TEXT.sourceGlassUnknown).length, 1);
  // No hint rests on a capacity nobody supplied.
  assert.equal(stepItems(body)[0].byClass("measure-hint").length, 0);

  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await page.settle();

  assert.equal(
    page.env.net.requests("GET", `/api/favorites/${H.FAV_1}`).slice(-1)[0].url,
    `/api/favorites/${H.FAV_1}?source_glass_ml=250`
  );
  assert.ok(stepItems(page.el("recipe-body"))[0].textContent.includes("1 стакан (2 ваших стакана)"));
  assert.equal(page.env.net.requests("PATCH", "/api/favorites").length, 0);
});

scenario("the_safe_unknown_answer_closes_the_question_and_keeps_only_declared_hints", async () => {
  const step = "Добавьте 500 мл воды.";
  const ingredients = [
    { name: "вода", amount: "1 стакан" },
    { name: "мука", amount: "1 стакан 200 мл" },
  ];
  const declaredLine = {
    position: 1,
    source: "1 стакан 200 мл",
    source_kind: "household",
    target_unit: "glass",
    status: "nominal_household",
    reason: "converted",
    exact: { numerator: 1, denominator: 2 },
    rounded: null,
    identical: false,
    presentation: "exact",
    source_terms: [
      {
        unit: "glass",
        amount: { numerator: 1, denominator: 1 },
        capacity_ml: { numerator: 200, denominator: 1 },
        capacity_origin: "declared_text",
      },
    ],
  };
  const glassLine = (origin, extra = {}) => ({
    position: 0,
    source: "1 стакан",
    source_kind: "household",
    target_unit: "glass",
    status: "clarification_required",
    reason: "glass_size_required",
    exact: null,
    rounded: null,
    identical: false,
    presentation: "none",
    source_terms: [
      {
        unit: "glass",
        amount: { numerator: 1, denominator: 1 },
        capacity_ml: null,
        capacity_origin: origin,
      },
    ],
    ...extra,
  });
  const convertedLine = glassLine("temporary_clarification", {
    status: "nominal_household",
    reason: "converted",
    exact: { numerator: 4, denominator: 5 },
    presentation: "exact",
  });
  const unclarified = H.stepBlock(
    3,
    [
      H.stepAnnotation(0, step, [
        H.span(step, "500 мл", {
          capacity_origin: "unresolved",
          conversion: null,
          conversion_omitted_reason: null,
        }),
      ]),
    ],
    {
      source_glass_required: true,
      steps_source_glass_required: true,
      ingredients: [glassLine("unresolved"), declaredLine],
    }
  );
  const clarified = H.stepBlock(
    3,
    [H.stepAnnotation(0, step, [H.span(step, "500 мл", { capacity_origin: "temporary_clarification" })])],
    {
      source_glass_ml: 200,
      source_glass_required: true,
      steps_source_glass_required: true,
      ingredients: [convertedLine, declaredLine],
    }
  );
  const card = (block) => structuredFavoriteItem({ steps: [step], ingredients, measures: block });
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([card(unclarified)]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(card(unclarified), "Борщ"),
        H.favoriteDetail(card(clarified), "Борщ"),
        H.favoriteDetail(card(unclarified), "Борщ")
      );
    },
    { structured: true }
  );
  await openCard(page);

  page.el("source-glass-200").checked = true;
  page.el("source-glass-200").fire("change");
  await page.settle();
  let body = page.el("recipe-body");
  assert.ok(body.textContent.includes("вода — 1 стакан"));
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"), "the clarified line");
  assert.ok(body.textContent.includes("Точно: ½ вашего стакана"), "the declared line");
  assert.ok(stepItems(body)[0].textContent.includes("500 мл (2 ваших стакана)"));

  page.el("source-glass-unknown").click();
  await page.settle();

  body = page.el("recipe-body");
  // The question is closed, with nothing drawn from the withdrawn capacity.
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassQuestion));
  assert.ok(!body.textContent.includes(Logic.TEXT.sourceGlassExplanation));
  assert.ok(!body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.equal(stepItems(body)[0].byClass("measure-hint").length, 0);
  // The declared conversion and every original amount are still there.
  assert.ok(body.textContent.includes("Точно: ½ вашего стакана"));
  assert.ok(body.textContent.includes("вода — 1 стакан"));
  assert.ok(body.textContent.includes("мука — 1 стакан 200 мл"));
  assert.equal(textOnly(stepItems(body)[0]), step);
  // Nothing was written, and the re-read carried no capacity.
  assert.equal(page.env.net.requests("PATCH", "/api/favorites").length, 0);
  assert.equal(page.env.net.requests("POST", "/api/favorites").length, 0);
  assert.equal(
    page.env.net.requests("GET", `/api/favorites/${H.FAV_1}`).slice(-1)[0].url,
    `/api/favorites/${H.FAV_1}`
  );

  // Reopening the recipe is a new view: the question comes back.
  await openCard(page);
  assert.ok(page.el("recipe-body").textContent.includes(Logic.TEXT.sourceGlassQuestion));
});

scenario("a_failed_reread_after_the_safe_answer_leaves_only_the_declared_hints", async () => {
  const step = "Добавьте 500 мл воды.";
  const ingredients = [
    { name: "вода", amount: "1 стакан" },
    { name: "мука", amount: "1 стакан 200 мл" },
  ];
  const declaredLine = {
    position: 1,
    source: "1 стакан 200 мл",
    source_kind: "household",
    target_unit: "glass",
    status: "nominal_household",
    reason: "converted",
    exact: { numerator: 1, denominator: 2 },
    rounded: null,
    identical: false,
    presentation: "exact",
    source_terms: [
      {
        unit: "glass",
        amount: { numerator: 1, denominator: 1 },
        capacity_ml: { numerator: 200, denominator: 1 },
        capacity_origin: "declared_text",
      },
    ],
  };
  const glassLine = (origin, extra = {}) => ({
    position: 0,
    source: "1 стакан",
    source_kind: "household",
    target_unit: "glass",
    status: "clarification_required",
    reason: "glass_size_required",
    exact: null,
    rounded: null,
    identical: false,
    presentation: "none",
    source_terms: [
      {
        unit: "glass",
        amount: { numerator: 1, denominator: 1 },
        capacity_ml: null,
        capacity_origin: origin,
      },
    ],
    ...extra,
  });
  const unclarified = H.stepBlock(
    3,
    [
      H.stepAnnotation(0, step, [
        H.span(step, "500 мл", {
          capacity_origin: "unresolved",
          conversion: null,
          conversion_omitted_reason: null,
        }),
      ]),
    ],
    { steps_source_glass_required: true, ingredients: [glassLine("unresolved"), declaredLine] }
  );
  const clarified = H.stepBlock(
    3,
    [H.stepAnnotation(0, step, [H.span(step, "500 мл", { capacity_origin: "temporary_clarification" })])],
    {
      source_glass_ml: 200,
      steps_source_glass_required: true,
      ingredients: [
        glassLine("temporary_clarification", {
          status: "nominal_household",
          reason: "converted",
          exact: { numerator: 4, denominator: 5 },
          presentation: "exact",
        }),
        declaredLine,
      ],
    }
  );
  const card = (block) => structuredFavoriteItem({ steps: [step], ingredients, measures: block });
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([card(unclarified)]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(card(unclarified), "Борщ"),
        H.favoriteDetail(card(clarified), "Борщ"),
        fail()
      );
    },
    { structured: true }
  );
  await openCard(page);
  page.el("source-glass-200").checked = true;
  page.el("source-glass-200").fire("change");
  await page.settle();
  assert.ok(page.el("recipe-body").textContent.includes("Точно: 0,8 вашего стакана"));

  page.el("source-glass-unknown").click();
  await page.settle();

  const body = page.el("recipe-body");
  // The cleared capacity is not drawn again just because the server could not be asked.
  assert.ok(!body.textContent.includes("Точно: 0,8 вашего стакана"));
  assert.equal(stepItems(body)[0].byClass("measure-hint").length, 0);
  assert.ok(body.textContent.includes("Точно: ½ вашего стакана"));
  assert.ok(body.textContent.includes("Добавьте 500 мл воды."));
  assert.equal(body.buttons(Logic.TEXT.retry).length, 1, "the failed read can be asked again");
});

scenario("a_failed_analysis_is_said_in_words_and_no_speculative_hint_is_drawn", async () => {
  const step = "Добавьте 500 мл воды.";
  const block = H.stepBlock(3, [], {
    analysis: {
      status: "failed",
      spans_emitted: 0,
      spans_omitted: 0,
      omitted_reasons: { limit_exceeded: 0, malformed: 0, unsafe_fraction: 0 },
      limits: { max_spans_per_step: 32, max_spans_per_recipe: 128, max_step_chars: 1000, max_steps: 50 },
    },
    ingredients: [
      {
        position: 0,
        source: "1 стакан 200 мл",
        source_kind: "household",
        target_unit: "glass",
        status: "nominal_household",
        reason: "converted",
        exact: { numerator: 4, denominator: 5 },
        rounded: null,
        identical: false,
        presentation: "exact",
      },
    ],
  });
  const page = await open(stepsCard(block, [step], { ingredients: [{ name: "вода", amount: "1 стакан 200 мл" }] }), {
    structured: true,
  });
  await openCard(page);

  const body = page.el("recipe-body");
  assert.ok(body.textContent.includes(Logic.TEXT.stepsNotChecked));
  assert.equal(stepItems(body)[0].textContent, step, "the stored step, with no derived number");
  // The ingredient hint the server did compute is untouched.
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
  // Nothing claims the recipe was checked or is consistent.
  assert.ok(!/проверен|подтвержд|согласован/i.test(body.textContent));
});

scenario("hostile_text_in_a_step_is_still_text_even_beside_a_hint", async () => {
  const step = "<img src=x onerror=alert(1)> Добавьте 500 мл воды.";
  const page = await open(
    stepsCard(H.stepBlock(3, [H.stepAnnotation(0, step, [H.span(step, "500 мл")])]), [step]),
    { structured: true }
  );
  await openCard(page);

  const body = page.el("recipe-body");
  const [item] = stepItems(body);
  assert.equal(textOnly(item), step);
  assert.ok(item.textContent.includes("500 мл (2 ваших стакана)"));
  assert.equal(body.byTag("img").length, 0);
  assert.equal(body.byTag("script").length, 0);
});

scenario("a_block_of_another_version_draws_no_step_hint_but_keeps_the_ingredient_ones", async () => {
  const step = "Добавьте 500 мл воды.";
  const block = H.stepBlock(2, [H.stepAnnotation(0, step, [H.span(step, "500 мл")])], {
    ingredients: [
      {
        position: 0,
        source: "1 стакан 200 мл",
        source_kind: "household",
        target_unit: "glass",
        status: "nominal_household",
        reason: "converted",
        exact: { numerator: 4, denominator: 5 },
        rounded: null,
        identical: false,
        presentation: "exact",
      },
    ],
  });
  const page = await open(stepsCard(block, [step], { ingredients: [{ name: "вода", amount: "1 стакан 200 мл" }] }), {
    structured: true,
  });
  await openCard(page);

  const body = page.el("recipe-body");
  assert.equal(stepItems(body)[0].textContent, step);
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));
});

scenario("the_profile_carries_the_approved_badge_and_measure_labels", async () => {
  const page = await open((env) => {
    env.signedIn([]);
    env.net.route("GET", "/api/profile", H.profileBody("Елена", "normal", [], { glass_ml: 240 }));
  });
  page.el("profile-open").click();
  await page.settle();
  await page.settle();

  const body = page.el("profile-body");
  assert.ok(body.textContent.includes("Указано вами"));
  assert.ok(body.textContent.includes("Объем моего стакана, мл"));
  assert.ok(body.textContent.includes("Объем моей чайной ложки, мл"));
  assert.ok(body.textContent.includes("Объем моей столовой ложки, мл"));
  assert.ok(!body.textContent.includes("ваш размер"));
  assert.ok(!body.textContent.includes("Объём"), "the UI spelling is Объем, as everywhere else");
  assert.ok(body.textContent.includes("240 мл"));
});

// ============================================================================================
// KH-F01-B1.1-D-REMEDIATION-01: rendering regressions for the audit findings
// ============================================================================================

// A conversion object the page reads as a number: an exact rational or a rounded one.
function exactConversion(numerator, denominator, extra = {}) {
  return {
    target_unit: "glass",
    status: "nominal_household",
    reason: "converted",
    exact: { numerator, denominator },
    rounded: null,
    identical: false,
    presentation: "exact",
    ...extra,
  };
}

const declaredTerm = {
  unit: "glass",
  amount: { numerator: 1, denominator: 1 },
  capacity_ml: { numerator: 200, denominator: 1 },
  capacity_origin: "declared_text",
};
const metricTerm = {
  unit: "milliliter",
  amount: { numerator: 500, denominator: 1 },
  capacity_ml: null,
  capacity_origin: "not_applicable",
};
const temporaryTerm = (capacity) => ({
  unit: "glass",
  amount: { numerator: 1, denominator: 1 },
  capacity_ml: { numerator: capacity, denominator: 1 },
  capacity_origin: "temporary_clarification",
});
const unresolvedTerm = {
  unit: "glass",
  amount: { numerator: 1, denominator: 1 },
  capacity_ml: null,
  capacity_origin: "unresolved",
};

function ingredientConversion(position, source, terms, extra = {}) {
  return {
    position,
    source,
    source_kind: "household",
    target_unit: "glass",
    status: "nominal_household",
    reason: "converted",
    exact: { numerator: 1, denominator: 2 },
    rounded: null,
    identical: false,
    presentation: "exact",
    source_terms: terms,
    ...extra,
  };
}

// The five-hint payload of the audit's D-R05 reproduction: three ingredient hints (one
// temporary-dependent, one declared, one metric) and two step hints (one temporary, one
// metric), all for recipe version 3.
const HINTED_STEP = "Добавьте 1 стакан воды и 500 мл молока.";
const HINTED_INGREDIENTS = [
  { name: "вода", amount: "1 стакан" },
  { name: "мука", amount: "1 стакан 200 мл" },
  { name: "молоко", amount: "500 мл" },
];

function hintedBlock({ clarified, terms = temporaryTerm(200) }) {
  const tempLine = ingredientConversion(0, "1 стакан", [terms], {
    status: clarified ? "nominal_household" : "clarification_required",
    reason: clarified ? "converted" : "glass_size_required",
    exact: clarified ? { numerator: 4, denominator: 5 } : null,
    presentation: clarified ? "exact" : "none",
  });
  const declaredLine = ingredientConversion(1, "1 стакан 200 мл", [declaredTerm], {
    exact: { numerator: 1, denominator: 2 },
    presentation: "exact",
  });
  const metricLine = ingredientConversion(2, "500 мл", [metricTerm], {
    source_kind: "metric_volume",
    exact: { numerator: 2, denominator: 1 },
    presentation: "exact",
  });
  const tempSpan = H.span(HINTED_STEP, "1 стакан", {
    ingredient_position: 0,
    capacity_origin: clarified ? "temporary_clarification" : "unresolved",
    conversion: clarified
      ? exactConversion(3, 5)
      : exactConversion(0, 1, {
          status: "clarification_required",
          reason: "glass_size_required",
          exact: null,
          presentation: "none",
        }),
  });
  const metricSpan = H.span(HINTED_STEP, "500 мл", {
    ingredient_position: 2,
    capacity_origin: "not_applicable",
    conversion: exactConversion(2, 1),
  });
  return H.stepBlock(
    3,
    [H.stepAnnotation(0, HINTED_STEP, [tempSpan, metricSpan])],
    {
      source_glass_ml: clarified ? 200 : null,
      source_glass_required: true,
      steps_source_glass_required: true,
      ingredients: [tempLine, declaredLine, metricLine],
    }
  );
}

function hintedCard(block) {
  return structuredFavoriteItem({ steps: [HINTED_STEP], ingredients: HINTED_INGREDIENTS, measures: block });
}

function hintedPage(env, { third }) {
  env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
  env.net.route("GET", "/api/favorites", H.favoritesBody([hintedCard(hintedBlock({ clarified: false }))]));
  env.net.route(
    "GET",
    `/api/favorites/${H.FAV_1}`,
    H.favoriteDetail(hintedCard(hintedBlock({ clarified: false })), "Борщ"),
    H.favoriteDetail(hintedCard(hintedBlock({ clarified: true })), "Борщ"),
    third
  );
}

scenario("the_safe_refresh_never_blanks_the_independent_hints_on_the_card", async () => {
  const pending = deferred();
  const page = await open((env) => hintedPage(env, { third: () => pending.promise }), { structured: true });
  await openCard(page);

  page.el("source-glass-200").checked = true;
  page.el("source-glass-200").fire("change");
  await page.settle();
  let body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 5, "the clarified block has five hints");
  assert.ok(body.textContent.includes("0,8 вашего стакана"));
  assert.ok(body.textContent.includes("0,6 вашего стакана"));

  // «Не знаю» while the no-parameter read is out: only the temporary-dependent hints go.
  page.el("source-glass-unknown").click();
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3, "5 -> 3, never 5 -> 0");
  assert.ok(!body.textContent.includes("0,8 вашего стакана"), "the temporary ingredient hint");
  assert.ok(!body.textContent.includes("0,6 вашего стакана"), "the temporary step hint");
  assert.ok(body.textContent.includes("½ вашего стакана"), "the declared ingredient hint");
  assert.ok(body.textContent.includes("2 ваших стакана"), "the metric hints, ingredient and step");
  assert.ok(body.textContent.includes(Logic.TEXT.measuresRefreshPending), "the read is named");
  assert.equal(textOnly(stepItems(body)[0]), HINTED_STEP, "the recipe stays readable throughout");

  // The authoritative answer replaces the block; the temporary hints stay gone.
  pending.resolve(H.favoriteDetail(hintedCard(hintedBlock({ clarified: false })), "Борщ"));
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3);
  assert.ok(!body.textContent.includes("0,8 вашего стакана"));
  assert.ok(body.textContent.includes("½ вашего стакана"));
  assert.ok(!body.textContent.includes(Logic.TEXT.measuresRefreshPending));
});

scenario("a_failed_safe_refresh_keeps_exactly_the_independent_hints", async () => {
  const page = await open((env) => hintedPage(env, { third: fail() }), { structured: true });
  await openCard(page);
  page.el("source-glass-200").checked = true;
  page.el("source-glass-200").fire("change");
  await page.settle();
  assert.equal(page.el("recipe-body").byClass("measure-hint").length, 5);

  page.el("source-glass-unknown").click();
  await page.settle();

  const body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3);
  assert.ok(!body.textContent.includes("0,8 вашего стакана"));
  assert.ok(!body.textContent.includes("0,6 вашего стакана"));
  assert.ok(body.textContent.includes("½ вашего стакана"));
  assert.ok(body.textContent.includes("2 ваших стакана"));
  assert.equal(body.buttons(Logic.TEXT.retry).length, 1, "the failed read can be asked again");
});

scenario("an_unverifiable_source_term_does_not_keep_its_hint_after_the_safe_answer", async () => {
  // The audit's D-R02 reproduction: the compound quantity's derived hint rests partly on
  // the temporary capacity, and the metadata says nothing usable about it.
  const compound = ingredientConversion(0, "1 стакан (200 мл) + 1 стакан", null, {
    exact: null,
    rounded: { numerator: 7, denominator: 4, relative_error: { numerator: 1, denominator: 100 } },
    presentation: "rounded",
  });
  const declared = ingredientConversion(1, "1 стакан 200 мл", [declaredTerm], {
    exact: { numerator: 4, denominator: 5 },
    presentation: "exact",
  });
  const ingredients = [
    { name: "вода", amount: "1 стакан (200 мл) + 1 стакан" },
    { name: "мука", amount: "1 стакан 200 мл" },
  ];
  const block = (capacity) =>
    H.stepBlock(3, [], {
      source_glass_ml: capacity,
      source_glass_required: true,
      ingredients: [compound, declared],
    });
  const card = (capacity) => structuredFavoriteItem({ steps: ["Смешайте."], ingredients, measures: block(capacity) });
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([card(null)]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(card(null), "Борщ"),
        H.favoriteDetail(card(250), "Борщ"),
        fail()
      );
    },
    { structured: true }
  );
  await openCard(page);

  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await page.settle();
  let body = page.el("recipe-body");
  assert.ok(body.textContent.includes("≈ 1¾ вашего стакана"), "the clarified conversion, before the refusal");
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"));

  page.el("source-glass-unknown").click();
  await page.settle();

  body = page.el("recipe-body");
  assert.ok(
    !body.textContent.includes("≈ 1¾ вашего стакана"),
    "a hint that may rest on the withdrawn capacity is not trusted without source terms"
  );
  assert.ok(body.textContent.includes("Точно: 0,8 вашего стакана"), "the independent declared hint stays");
  assert.ok(body.textContent.includes("вода — 1 стакан (200 мл) + 1 стакан"));
  assert.ok(body.textContent.includes("мука — 1 стакан 200 мл"));
});

// The exact D-R02 mutation, on the five-hint page: with the 250 mL temporary capacity in
// effect the real Stage C payload reports the second term of `1 стакан (200 мл) + 1 стакан`
// as `temporary_clarification`; this payload says `unresolved` (no capacity at all) and keeps
// the successful conversion the 250 mL produced. Stage C never emits that combination (an
// undeclared glass without a clarification is `clarification_required`), so the number is
// refused outright -- it is never drawn, even while the capacity is confirmed on the card.
const MUTATED_STEP = "Добавьте 1 стакан воды и 500 мл молока.";
const MUTATED_INGREDIENTS = [
  { name: "вода", amount: "1 стакан (200 мл) + 1 стакан" },
  { name: "мука", amount: "1 стакан 200 мл" },
  { name: "молоко", amount: "500 мл" },
];

// `secondTerm(capacity)` is the resolved second source term: `temporaryTerm(250)` for the
// valid Stage C answer, `unresolvedTerm` for the mutation.
function compoundCard(capacity, secondTerm = () => unresolvedTerm) {
  const conversion = capacity === null
    ? { status: "clarification_required", reason: "glass_size_required", exact: null, presentation: "none" }
    : {
        exact: null,
        rounded: { numerator: 7, denominator: 4, relative_error: { numerator: 1, denominator: 100 } },
        presentation: "rounded",
      };
  const compound = ingredientConversion(
    0,
    "1 стакан (200 мл) + 1 стакан",
    [declaredTerm, secondTerm(capacity)],
    conversion
  );
  const declaredLine = ingredientConversion(1, "1 стакан 200 мл", [declaredTerm], {
    exact: { numerator: 1, denominator: 2 },
    presentation: "exact",
  });
  const metricLine = ingredientConversion(2, "500 мл", [metricTerm], {
    source_kind: "metric_volume",
    exact: { numerator: 2, denominator: 1 },
    presentation: "exact",
  });
  const block = H.stepBlock(
    3,
    [
      H.stepAnnotation(0, MUTATED_STEP, [
        H.span(MUTATED_STEP, "1 стакан", {
          ingredient_position: 0,
          capacity_origin: capacity === null ? "unresolved" : "temporary_clarification",
          conversion:
            capacity === null
              ? exactConversion(0, 1, {
                  status: "clarification_required",
                  reason: "glass_size_required",
                  exact: null,
                  presentation: "none",
                })
              : exactConversion(3, 5),
        }),
        H.span(MUTATED_STEP, "500 мл", {
          ingredient_position: 2,
          capacity_origin: "not_applicable",
          conversion: exactConversion(2, 1),
        }),
      ]),
    ],
    {
      source_glass_ml: capacity,
      source_glass_required: true,
      steps_source_glass_required: true,
      ingredients: [compound, declaredLine, metricLine],
    }
  );
  return structuredFavoriteItem({ steps: [MUTATED_STEP], ingredients: MUTATED_INGREDIENTS, measures: block });
}

scenario("the_codex_unresolved_term_payload_never_draws_its_number", async () => {
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([compoundCard(null)]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(compoundCard(null), "Борщ"),
        H.favoriteDetail(compoundCard(250), "Борщ"),
        fail()
      );
    },
    { structured: true }
  );
  await openCard(page);

  let body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3, "the unclarified payload has three hints");
  assert.ok(!body.textContent.includes("≈ 1¾ вашего стакана"));
  assert.ok(body.textContent.includes("вода — 1 стакан (200 мл) + 1 стакан"), "the stored text");

  // Even with 250 mL confirmed, the contradictory conversion is refused: four hints, and the
  // refused number is not among them.
  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 4, "the clarified block draws four hints");
  assert.ok(
    !body.textContent.includes("≈ 1¾ вашего стакана"),
    "a successful conversion beside an unresolved term is never drawn"
  );
  // The valid ones are untouched, including the step hint supplied by this clarification.
  assert.ok(body.textContent.includes("0,6 вашего стакана"), "the temporary step hint");
  assert.ok(body.textContent.includes("½ вашего стакана"), "the independent declared hint");
  assert.ok(body.textContent.includes("2 ваших стакана"), "the independent metric hints");

  // «Не знаю» while the no-parameter read is out and fails: the temporary step hint goes,
  // and exactly the three independently valid hints remain.
  page.el("source-glass-unknown").click();
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3, "three independent hints, before and after");
  assert.ok(!body.textContent.includes("≈ 1¾ вашего стакана"), "the refused number stays gone");
  assert.ok(!body.textContent.includes("0,6 вашего стакана"), "the temporary step hint");
  assert.ok(body.textContent.includes("½ вашего стакана"), "the independent declared hint stays");
  assert.ok(body.textContent.includes("2 ваших стакана"), "the independent metric hints stay");
  assert.equal(body.buttons(Logic.TEXT.retry).length, 1, "the failed read can be asked again");
  assert.equal(textOnly(stepItems(body)[0]), MUTATED_STEP, "the recipe stays readable");
});

// The same mutation with the capacity the clarification supplied still written beside the
// unresolved origin. The origin alone decides -- `unresolved` says no capacity was ever
// established -- so the number it produced may not be drawn whatever `capacity_ml` says.
const unresolvedWithCapacity = (capacity) => ({
  ...unresolvedTerm,
  capacity_ml: { numerator: capacity, denominator: 1 },
});

scenario("an_unresolved_term_that_keeps_a_capacity_still_refuses_its_number", async () => {
  const pending = deferred();
  const kept = () => unresolvedWithCapacity(250);
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([compoundCard(null, kept)]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(compoundCard(null, kept), "Борщ"),
        H.favoriteDetail(compoundCard(250, kept), "Борщ"),
        () => pending.promise
      );
    },
    { structured: true }
  );
  await openCard(page);

  // The unclarified answer: three hints, and none of them the compound line's number.
  let body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3, "the unclarified payload has three hints");
  assert.ok(!body.textContent.includes("≈ 1¾ вашего стакана"));

  // Even with 250 mL confirmed -- and still written beside the unresolved origin -- the
  // contradictory conversion is refused: the four independently valid hints remain, and the
  // refused number is never drawn.
  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 4, "the clarified block draws four hints");
  assert.ok(
    !body.textContent.includes("≈ 1¾ вашего стакана"),
    "an unresolved origin keeps no number beside any capacity"
  );
  assert.ok(body.textContent.includes("0,6 вашего стакана"), "the temporary step hint");
  assert.ok(body.textContent.includes("½ вашего стакана"), "the independent declared hint");
  assert.ok(body.textContent.includes("2 ваших стакана"), "the independent metric hints");

  // «Не знаю» while the no-parameter read is still out: the temporary step hint goes, and
  // exactly the three independent hints remain.
  page.el("source-glass-unknown").click();
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3, "three independent hints during the safe read");
  assert.ok(!body.textContent.includes("≈ 1¾ вашего стакана"), "the refused number stays gone");
  assert.ok(!body.textContent.includes("0,6 вашего стакана"), "the temporary step hint");
  assert.ok(body.textContent.includes("½ вашего стакана"));
  assert.ok(body.textContent.includes("2 ваших стакана"));
  assert.ok(body.textContent.includes(Logic.TEXT.measuresRefreshPending), "the read is named");

  // The read fails: the old block stays, and still exactly three hints are drawn.
  pending.resolve(fail());
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3, "three independent hints after the failed read");
  assert.ok(!body.textContent.includes("≈ 1¾ вашего стакана"), "the refused number stays gone");
  assert.ok(!body.textContent.includes("0,6 вашего стакана"));
  assert.ok(body.textContent.includes("½ вашего стакана"));
  assert.ok(body.textContent.includes("2 ваших стакана"));
  assert.equal(body.buttons(Logic.TEXT.retry).length, 1, "the failed read can be asked again");
  assert.equal(textOnly(stepItems(body)[0]), MUTATED_STEP, "the recipe stays readable");
});

scenario("a_valid_clarified_compound_still_draws_all_five_hints", async () => {
  // The unmodified Stage C payload: the second term of the compound is
  // `temporary_clarification`, so the number it produced is the one the person confirmed.
  const terms = (capacity) => (capacity === null ? unresolvedTerm : temporaryTerm(capacity));
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([compoundCard(null, terms)]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(compoundCard(null, terms), "Борщ"),
        H.favoriteDetail(compoundCard(250, terms), "Борщ"),
        fail()
      );
    },
    { structured: true }
  );
  await openCard(page);

  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await page.settle();
  let body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 5, "the valid clarified payload draws five");
  assert.ok(body.textContent.includes("≈ 1¾ вашего стакана"));

  // The safe answer withdraws the temporary capacity, and with it only the hints that rest
  // on it: five become three, never zero.
  page.el("source-glass-unknown").click();
  await page.settle();
  body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3, "5 -> 3");
  assert.ok(!body.textContent.includes("≈ 1¾ вашего стакана"));
  assert.ok(!body.textContent.includes("0,6 вашего стакана"));
  assert.ok(body.textContent.includes("½ вашего стакана"));
  assert.ok(body.textContent.includes("2 ваших стакана"));
});

scenario("an_authoritative_no_parameter_refresh_for_the_mutated_payload_restores_only_valid_hints", async () => {
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([compoundCard(null)]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(compoundCard(null), "Борщ"),
        H.favoriteDetail(compoundCard(250), "Борщ"),
        H.favoriteDetail(compoundCard(null), "Борщ")
      );
    },
    { structured: true }
  );
  await openCard(page);

  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await page.settle();
  assert.equal(page.el("recipe-body").byClass("measure-hint").length, 4);

  page.el("source-glass-unknown").click();
  await page.settle();
  const body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 3, "the server's own unclarified block");
  assert.ok(!body.textContent.includes("≈ 1¾ вашего стакана"), "no number without a capacity");
  assert.ok(!body.textContent.includes("0,6 вашего стакана"));
  assert.ok(body.textContent.includes("½ вашего стакана"));
  assert.ok(body.textContent.includes("2 ваших стакана"));
  assert.ok(!body.textContent.includes(Logic.TEXT.measuresRefreshPending), "the read answered");
});

scenario("a_compound_conversion_with_one_temporary_term_is_suppressed_as_a_whole", async () => {
  // A declared term beside a temporary one does not rescue the line: any term may rest on
  // the withdrawn capacity, so the whole conversion is not drawn after the refusal.
  const block = (capacity) => {
    const compound = ingredientConversion(
      0,
      "1 стакан (200 мл) + 1 стакан",
      [declaredTerm, capacity === null ? unresolvedTerm : temporaryTerm(250)],
      capacity === null
        ? { status: "clarification_required", reason: "glass_size_required", exact: null, presentation: "none" }
        : {
            exact: null,
            rounded: { numerator: 7, denominator: 4, relative_error: { numerator: 1, denominator: 100 } },
            presentation: "rounded",
          }
    );
    const declaredLine = ingredientConversion(1, "1 стакан 200 мл", [declaredTerm], {
      exact: { numerator: 1, denominator: 2 },
      presentation: "exact",
    });
    return H.stepBlock(3, [], {
      source_glass_ml: capacity,
      source_glass_required: true,
      ingredients: [compound, declaredLine],
    });
  };
  const ingredients = [
    { name: "вода", amount: "1 стакан (200 мл) + 1 стакан" },
    { name: "мука", amount: "1 стакан 200 мл" },
  ];
  const card = (capacity) => structuredFavoriteItem({ ingredients, measures: block(capacity) });
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([card(null)]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(card(null), "Борщ"),
        H.favoriteDetail(card(250), "Борщ"),
        fail()
      );
    },
    { structured: true }
  );
  await openCard(page);
  page.el("source-glass-250").checked = true;
  page.el("source-glass-250").fire("change");
  await page.settle();
  assert.ok(page.el("recipe-body").textContent.includes("≈ 1¾ вашего стакана"));

  page.el("source-glass-unknown").click();
  await page.settle();
  const body = page.el("recipe-body");
  assert.ok(!body.textContent.includes("≈ 1¾ вашего стакана"), "the whole line goes");
  assert.ok(body.textContent.includes("½ вашего стакана"), "the declared line stays");
  assert.equal(body.byClass("measure-hint").length, 1);
});

scenario("a_fully_declared_compound_and_independent_spoons_survive_the_safe_answer", async () => {
  const spoonTerm = {
    unit: "tablespoon",
    amount: { numerator: 1, denominator: 1 },
    capacity_ml: null,
    capacity_origin: "not_applicable",
  };
  const spoons = ingredientConversion(1, "1 ст. л.", [spoonTerm], {
    target_unit: "tablespoon",
    exact: { numerator: 1, denominator: 1 },
    presentation: "exact",
  });
  const declaredCompound = ingredientConversion(0, "1 стакан 200 мл + 1 стакан 200 мл", [declaredTerm, declaredTerm], {
    exact: { numerator: 8, denominator: 5 },
    presentation: "exact",
  });
  const block = H.stepBlock(3, [], {
    source_glass_required: true,
    ingredients: [declaredCompound, spoons],
  });
  const ingredients = [
    { name: "вода", amount: "1 стакан 200 мл + 1 стакан 200 мл" },
    { name: "масло", amount: "1 ст. л." },
  ];
  const card = structuredFavoriteItem({ ingredients, measures: block });
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([card]));
      env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(card, "Борщ"));
    },
    { structured: true }
  );
  await openCard(page);
  const before = page.el("recipe-body").byClass("measure-hint").length;
  assert.equal(before, 2);

  page.el("source-glass-unknown").click();
  await page.settle();
  const body = page.el("recipe-body");
  assert.equal(body.byClass("measure-hint").length, 2, "nothing was withdrawn");
  assert.ok(body.textContent.includes("Точно: 1,6 вашего стакана"));
  assert.ok(body.textContent.includes("Точно: 1 ваша столовая ложка"));
});

scenario("each_failed_component_is_said_in_its_own_words", async () => {
  const step = "Добавьте 500 мл воды.";
  const goodSpan = H.span(step, "500 мл");
  const steps = [step];
  const ingredients = [{ name: "вода", amount: "500 мл" }];
  const ingredient = ingredientConversion(0, "500 мл", [metricTerm], {
    source_kind: "metric_volume",
    exact: { numerator: 2, denominator: 1 },
    presentation: "exact",
  });
  const failedAnalysis = {
    status: "failed",
    spans_emitted: 0,
    spans_omitted: 0,
    omitted_reasons: { limit_exceeded: 0, malformed: 0, unsafe_fraction: 0 },
    limits: { max_spans_per_step: 32, max_spans_per_recipe: 128, max_step_chars: 1000, max_steps: 50 },
  };
  const failedProvenance = { status: "failed", declared: [], incomplete: [] };

  async function card(block) {
    const page = await open(stepsCard(block, steps, { ingredients }), { structured: true });
    await openCard(page);
    return page.el("recipe-body");
  }

  // Provenance only: the step conversions are still there and are not denied.
  let body = await card(
    H.stepBlock(3, [H.stepAnnotation(0, step, [goodSpan])], {
      analysis: failedAnalysis,
      provenance: failedProvenance,
      ingredients: [ingredient],
    })
  );
  assert.ok(body.textContent.includes(Logic.TEXT.provenanceNotChecked));
  assert.ok(!body.textContent.includes(Logic.TEXT.stepsNotChecked), "no claim about step amounts");
  assert.ok(stepItems(body)[0].textContent.includes("2 ваших стакана"), "the step hint stays");
  assert.ok(body.textContent.includes("Точно: 2 ваших стакана"), "the ingredient hint stays");
  assert.ok(!/проверен|подтвержд|согласован/i.test(body.textContent), "no claim the recipe was checked");

  // Steps only: nothing is claimed about the ingredients, whose hint stays.
  body = await card(
    H.stepBlock(3, [], {
      analysis: failedAnalysis,
      ingredients: [ingredient],
    })
  );
  assert.ok(body.textContent.includes(Logic.TEXT.stepsNotChecked));
  assert.ok(!body.textContent.includes(Logic.TEXT.provenanceNotChecked));
  assert.ok(body.textContent.includes("Точно: 2 ваших стакана"));
  assert.ok(!/проверен|подтвержд|согласован/i.test(body.textContent));

  // Both: the provenance failure is named and the whole-block summary is said neutrally;
  // the client cannot tell a step-pass failure from a pass with no amounts.
  body = await card(
    H.stepBlock(3, [], {
      analysis: failedAnalysis,
      provenance: failedProvenance,
      ingredients: [ingredient],
    })
  );
  assert.ok(body.textContent.includes(Logic.TEXT.provenanceNotChecked));
  assert.ok(body.textContent.includes(Logic.TEXT.analysisPartlyFailed));
  assert.ok(!body.textContent.includes(Logic.TEXT.stepsNotChecked));
  assert.ok(!/проверен|подтвержд|согласован/i.test(body.textContent));
});

scenario("reopening_the_same_favorite_from_the_list_asks_without_the_old_capacity", async () => {
  const block = (capacity) =>
    H.stepBlock(3, [H.stepAnnotation(0, HINTED_STEP, [
      H.span(HINTED_STEP, "1 стакан", {
        ingredient_position: 0,
        capacity_origin: capacity === null ? "unresolved" : "temporary_clarification",
        conversion: capacity === null
          ? exactConversion(0, 1, {
              status: "clarification_required",
              reason: "glass_size_required",
              exact: null,
              presentation: "none",
            })
          : exactConversion(4, 5),
      }),
    ])], {
      source_glass_ml: capacity,
      source_glass_required: true,
      steps_source_glass_required: true,
      ingredients: [ingredientConversion(0, "1 стакан", capacity === null ? [unresolvedTerm] : [temporaryTerm(capacity)], {
        status: capacity === null ? "clarification_required" : "nominal_household",
        reason: capacity === null ? "glass_size_required" : "converted",
        exact: capacity === null ? null : { numerator: 4, denominator: 5 },
        presentation: capacity === null ? "none" : "exact",
      })],
    });
  const ingredients = [{ name: "вода", amount: "1 стакан" }];
  const card = (capacity) => structuredFavoriteItem({ steps: [HINTED_STEP], ingredients, measures: block(capacity) });
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      env.net.route("GET", "/api/favorites", H.favoritesBody([card(null)]));
      env.net.route(
        "GET",
        `/api/favorites/${H.FAV_1}`,
        H.favoriteDetail(card(null), "Борщ"),
        H.favoriteDetail(card(200), "Борщ"),
        H.favoriteDetail(card(null), "Борщ")
      );
    },
    { structured: true }
  );
  await openCard(page);
  page.el("source-glass-200").checked = true;
  page.el("source-glass-200").fire("change");
  await page.settle();
  assert.ok(page.el("recipe-body").textContent.includes("0,8 вашего стакана"));
  assert.equal(
    page.env.net.requests("GET", `/api/favorites/${H.FAV_1}`).slice(-1)[0].url,
    `/api/favorites/${H.FAV_1}?source_glass_ml=200`
  );

  // The same favorite, clicked again in the list, is a new view: no capacity is carried.
  await openCard(page);
  assert.equal(
    page.env.net.requests("GET", `/api/favorites/${H.FAV_1}`).slice(-1)[0].url,
    `/api/favorites/${H.FAV_1}`,
    "the reopened view asks for the recipe as its own text declares it"
  );
  assert.ok(!page.el("recipe-body").textContent.includes("0,8 вашего стакана"));
  assert.ok(page.el("recipe-body").textContent.includes(Logic.TEXT.sourceGlassQuestion));
});

// ============================================================================================
// KH-F01-B2-3: ingredient-specific mass estimates on the saved card
// ============================================================================================
//
// The payloads here are synthetic fixtures of the accepted B2-2 shape. The shipped registry
// approves no record, so a real recipe may legitimately show no mass hint at all; nothing
// below is a real coefficient.

const MASS_INGREDIENTS = [
  { name: "мука", amount: "125 г" },
  { name: "сахар", amount: "200 г" },
  { name: "соль", amount: "5 г" },
  { name: "масло", amount: "30 г" },
];

function massVolumeItem(position, rounded, conversionExtra = {}, extra = {}) {
  return {
    position,
    name: MASS_INGREDIENTS[position].name,
    source: MASS_INGREDIENTS[position].amount,
    mass_g: { numerator: 1, denominator: 1 },
    ingredient: { id: "test_ingredient", display_name: "тестовый ингредиент", match: "matched" },
    status: "reference_approximate",
    reason: "converted",
    conditional: false,
    assumption: null,
    conversion: {
      kind: "volume",
      target_unit: "glass",
      exact: { numerator: 1, denominator: 1 },
      rounded: {
        numerator: rounded.numerator,
        denominator: rounded.denominator,
        relative_error: { numerator: 0, denominator: 1 },
      },
      reference: {
        evidence_id: "fixture.test",
        derivation: "direct_target",
        normalization: null,
        method: null,
      },
      ...conversionExtra,
    },
    ...extra,
  };
}

function massCountItem(position, conversion = {}, extra = {}) {
  return {
    position,
    name: MASS_INGREDIENTS[position].name,
    source: MASS_INGREDIENTS[position].amount,
    mass_g: { numerator: 1, denominator: 1 },
    ingredient: { id: "test_ingredient", display_name: "тестовый ингредиент", match: "matched" },
    status: "reference_approximate",
    reason: "converted",
    conditional: false,
    assumption: null,
    conversion: {
      kind: "count",
      piece: { id: "test_piece", size_class: null, reference_kind: "point" },
      display: "single",
      count_low: { numerator: 3, denominator: 1 },
      count_high: { numerator: 3, denominator: 1 },
      rounded_low: { numerator: 3, denominator: 1 },
      rounded_high: { numerator: 3, denominator: 1 },
      relative_width: { numerator: 0, denominator: 1 },
      reference: { evidence_id: "fixture.test" },
      ...conversion,
    },
    ...extra,
  };
}

function massSection(items, extra = {}) {
  return {
    schema_version: 1,
    registry_revision: "2026-11-14.test",
    status: "complete",
    items,
    omitted: { unsafe_fraction: 0, malformed: 0 },
    ...extra,
  };
}

function massCard(blockOrNull, b1 = [], ingredients = MASS_INGREDIENTS) {
  const block =
    blockOrNull === null
      ? H.stepBlock(3, [], { ingredients: b1 })
      : H.stepBlock(3, [], { ingredients: b1, mass_conversions: blockOrNull });
  return structuredFavoriteItem({ ingredients, measures: block });
}

async function openMassCard(blockOrNull, b1 = [], ingredients = MASS_INGREDIENTS) {
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      const item = massCard(blockOrNull, b1, ingredients);
      env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
      env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ"));
    },
    { structured: true }
  );
  await openCard(page);
  return page;
}

const massLines = (page) => page.el("recipe-body").byClass("recipe-list")[0].byTag("li");
const massHints = (page) => page.el("recipe-body").byClass("recipe-mass-hint");

scenario("a_mass_volume_estimate_is_drawn_under_its_own_ingredient_and_the_amount_is_unchanged", async () => {
  const page = await openMassCard(massSection([massVolumeItem(1, { numerator: 1, denominator: 1 })]));
  const lines = massLines(page);

  // The estimate belongs to the second line; the first line is untouched.
  assert.equal(lines[0].byClass("recipe-mass-hint").length, 0);
  assert.equal(lines[1].byClass("recipe-mass-hint").length, 1);
  assert.equal(lines[1].byClass("recipe-ingredient")[0].textContent, "сахар — 200 г");
  const hint = massHints(page)[0];
  assert.equal(hint.textContent, "≈ 1 стакан");
  // The approximation is said in words, not by colour alone, and the caption groups it with
  // the person's own measures.
  assert.ok(hint.textContent.includes("≈"));
  assert.equal(page.el("recipe-body").byClass("measure-caption")[0].textContent, Logic.TEXT.measuresTitle);
});

scenario("whole_fractional_and_mixed_measures_read_as_russian_kitchen_fractions", async () => {
  const page = await openMassCard(
    massSection([
      massVolumeItem(0, { numerator: 1, denominator: 4 }),
      massVolumeItem(1, { numerator: 3, denominator: 2 }),
      massVolumeItem(2, { numerator: 5, denominator: 1 }),
      massVolumeItem(3, { numerator: 1, denominator: 3 }, { target_unit: "teaspoon" }),
    ])
  );
  const texts = massHints(page).map((hint) => hint.textContent);
  assert.deepEqual(texts, [
    "≈ ¼ стакана",
    "≈ 1½ стакана",
    "≈ 5 стаканов",
    "≈ ⅓ чайной ложки",
  ]);
});

scenario("a_point_count_and_a_genuine_interval_are_drawn_apart", async () => {
  const page = await openMassCard(
    massSection([
      massCountItem(0),
      massCountItem(1, {
        piece: { id: "test_piece", size_class: "medium", reference_kind: "measured_interval" },
        display: "interval",
        count_low: { numerator: 50, denominator: 17 },
        count_high: { numerator: 10, denominator: 3 },
        rounded_low: { numerator: 3, denominator: 1 },
        rounded_high: { numerator: 7, denominator: 2 },
        relative_width: { numerator: 1, denominator: 8 },
      }),
    ])
  );
  const texts = massHints(page).map((hint) => hint.textContent);
  assert.deepEqual(texts, ["≈ 3 шт.", "≈ 3–3½ шт."]);
});

scenario("a_conditional_estimate_states_its_assumption_beside_the_number", async () => {
  const page = await openMassCard(
    massSection([
      massCountItem(
        0,
        {
          piece: { id: "test_piece", size_class: "medium", reference_kind: "calibration_class" },
          display: "single",
          count_low: { numerator: 50, denominator: 17 },
          count_high: { numerator: 25, denominator: 8 },
          relative_width: { numerator: 2, denominator: 33 },
        },
        {
          conditional: true,
          assumption: {
            kind: "size_class",
            size_class: "medium",
            weight_g: { numerator: 160, denominator: 1 },
            mass_basis: "edible_portion",
            label_ru: "если клубни средние",
          },
        }
      ),
    ])
  );
  const hint = massHints(page)[0];
  assert.equal(hint.textContent, "≈ 3 шт., если клубни средние");
  // The assumption is visible text, not a tooltip or an attribute only.
  assert.equal(hint.getAttribute("title"), null);
  assert.ok(hint.textContent.includes("если клубни средние"));
});

scenario("a_refused_conversion_draws_no_number_and_the_original_amount_stays", async () => {
  const page = await openMassCard(
    massSection([
      massVolumeItem(
        1,
        { numerator: 1, denominator: 1 },
        {},
        {
          status: "unsupported",
          reason: "reference_not_approved",
          ingredient: { id: null, display_name: null, match: "matched" },
          conversion: null,
        }
      ),
    ])
  );
  const lines = massLines(page);

  assert.equal(massHints(page).length, 0);
  assert.equal(lines[1].byClass("recipe-ingredient")[0].textContent, "сахар — 200 г");
  assert.equal(lines[1].textContent, "сахар — 200 г", "no number and no technical reason");
  assert.equal(page.el("recipe-body").textContent.includes("reference_not_approved"), false);
  assert.equal(page.el("recipe-body").byClass("measure-caption").length, 0);
});

scenario("a_missing_or_malformed_mass_extension_draws_no_number_and_keeps_b1_1", async () => {
  const b1 = [ingredientConversion(0, "125 г", [metricTerm], { source_kind: "metric_volume" })];
  const malformed = [
    null,
    { schema_version: 2, status: "complete", items: [], omitted: { unsafe_fraction: 0, malformed: 0 } },
    { schema_version: 1, status: "complete", items: "not a list" },
    massSection([
      massVolumeItem(0, { numerator: 1, denominator: 1 }, {
        rounded: { numerator: 1.5, denominator: 1, relative_error: { numerator: 0, denominator: 1 } },
      }),
    ]),
    massSection([massVolumeItem(0, { numerator: 1, denominator: 1 })], { status: "failed" }),
  ];
  for (const block of malformed) {
    const page = await openMassCard(block, b1);
    assert.equal(massHints(page).length, 0, JSON.stringify(block));
    // B1.1 is exactly as it was: one hint, drawn from its own section.
    assert.equal(page.el("recipe-body").byClass("measure-hint").length, 1, JSON.stringify(block));
    assert.ok(page.el("recipe-body").textContent.includes("½ вашего стакана"), JSON.stringify(block));
  }
});

scenario("a_mass_estimate_never_duplicates_a_b1_1_hint_for_the_same_line", async () => {
  const b1 = [ingredientConversion(0, "125 г", [metricTerm], { source_kind: "metric_volume" })];
  const page = await openMassCard(
    massSection([massVolumeItem(0, { numerator: 1, denominator: 1 })]),
    b1
  );

  assert.equal(page.el("recipe-body").byClass("measure-hint").length, 1);
  assert.equal(massHints(page).length, 0);
  assert.ok(page.el("recipe-body").textContent.includes("½ вашего стакана"));
});

scenario("a_long_ingredient_name_and_a_long_assumption_are_kept_whole_and_wrap", async () => {
  const label = "если используются клубни указанного в рецепте размера";
  const longName = "мука пшеничная цельнозерновая крупного помола высшего сорта";
  const item = massCountItem(
    0,
    {
      piece: { id: "test_piece", size_class: "large", reference_kind: "calibration_class" },
      display: "single",
      count_low: { numerator: 50, denominator: 17 },
      count_high: { numerator: 25, denominator: 8 },
      relative_width: { numerator: 2, denominator: 33 },
    },
    {
      name: longName,
      conditional: true,
      assumption: {
        kind: "size_class",
        size_class: "large",
        weight_g: { numerator: 160, denominator: 1 },
        mass_basis: "edible_portion",
        label_ru: label,
      },
    }
  );
  const page = await openMassCard(massSection([item]), [], [{ name: longName, amount: "125 г" }]);
  const line = massLines(page)[0];
  const hint = massHints(page)[0];

  // Neither the ingredient nor the assumption is shortened in the drawn text; the wrap is
  // the style's job (.recipe-mass-hint), and this mini DOM has no layout.
  assert.equal(line.byClass("recipe-ingredient")[0].textContent, longName + " — 125 г");
  assert.equal(hint.textContent, "≈ 3 шт., " + label);
  assert.equal(hint.getAttribute("title"), null, "nothing is hidden in a tooltip");
});

// --- KH-F01-B2-3-R1: exact interval order, ingredient/version binding, zero mass ------------

scenario("a_real_mass_count_interval_is_drawn_as_one_and_a_half_to_one_and_three_quarters", async () => {
  // The accepted B2-2 shape: 250 g at 150-160 g per piece is 25/16..5/3 exact, rounded to
  // 3/2..7/4. Before the comparison fix the page dropped this interval instead of drawing it.
  const ingredients = [{ name: "тестовый корень", amount: "250 г" }];
  const item = massCountItem(
    0,
    {
      piece: { id: "test_root_piece", size_class: null, reference_kind: "measured_interval" },
      display: "interval",
      count_low: { numerator: 25, denominator: 16 },
      count_high: { numerator: 5, denominator: 3 },
      rounded_low: { numerator: 3, denominator: 2 },
      rounded_high: { numerator: 7, denominator: 4 },
      relative_width: { numerator: 1, denominator: 12 },
    },
    { name: "тестовый корень", source: "250 г", mass_g: { numerator: 250, denominator: 1 } }
  );
  const page = await openMassCard(massSection([item]), [], ingredients);

  assert.equal(massHints(page).length, 1);
  assert.equal(massHints(page)[0].textContent, "≈ 1½–1¾ шт.");
  assert.equal(massLines(page)[0].byClass("recipe-ingredient")[0].textContent, "тестовый корень — 250 г");
});

scenario("a_mass_hint_written_for_another_ingredient_or_amount_is_not_drawn", async () => {
  // The right position, but another ingredient's name: no hint on this line.
  const wrongName = massVolumeItem(0, { numerator: 1, denominator: 1 }, {}, { name: "сахар", source: "200 г" });
  let page = await openMassCard(massSection([wrongName]));
  assert.equal(massHints(page).length, 0);
  assert.equal(massLines(page)[0].textContent, "мука — 125 г", "the stored line is untouched");

  // The right name, but another amount: no hint either.
  const wrongAmount = massVolumeItem(0, { numerator: 1, denominator: 1 }, {}, { source: "200 г" });
  page = await openMassCard(massSection([wrongAmount]));
  assert.equal(massHints(page).length, 0);

  // A wrong item costs only its own line: the genuine sibling is drawn, and B1.1 keeps its
  // own hint on the line it already spoke for.
  const b1 = [ingredientConversion(0, "125 г", [metricTerm], { source_kind: "metric_volume" })];
  const mixed = await openMassCard(
    massSection([
      massVolumeItem(0, { numerator: 1, denominator: 1 }, {}, { name: "сахар" }),
      massVolumeItem(1, { numerator: 1, denominator: 1 }),
    ]),
    b1
  );
  assert.equal(massHints(mixed).length, 1);
  assert.equal(massLines(mixed)[0].byClass("recipe-mass-hint").length, 0);
  assert.equal(massLines(mixed)[1].byClass("recipe-mass-hint")[0].textContent, "≈ 1 стакан");
  assert.ok(mixed.el("recipe-body").textContent.includes("½ вашего стакана"), "B1.1 untouched");
});

scenario("a_mass_extension_bound_to_another_version_draws_no_hint_in_the_page", async () => {
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      // The card is version 3; this block says it was built for version 2.
      const block = H.stepBlock(2, [], {
        ingredients: [],
        mass_conversions: massSection([massVolumeItem(1, { numerator: 1, denominator: 1 })]),
      });
      const item = structuredFavoriteItem({ ingredients: MASS_INGREDIENTS, measures: block });
      env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
      env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ"));
    },
    { structured: true }
  );
  await openCard(page);

  assert.equal(massHints(page).length, 0);
  assert.equal(massLines(page)[1].textContent, "сахар — 200 г");
});

// KH-F01-B2-3-R2: the real detail normalization and rendering path, with an arbitrary
// original top-level `version` on the favorite (`undefined` omits the key entirely).
async function openMassCardWithOriginalVersion(version, block, ingredients = MASS_INGREDIENTS) {
  const page = await open(
    (env) => {
      env.signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, structuredThread()) });
      const item = structuredFavoriteItem({ ingredients, measures: block });
      if (version === undefined) {
        delete item.version;
      } else {
        item.version = version;
      }
      env.net.route("GET", "/api/favorites", H.favoritesBody([item]));
      env.net.route("GET", `/api/favorites/${H.FAV_1}`, H.favoriteDetail(item, "Борщ"));
    },
    { structured: true }
  );
  await openCard(page);
  return page;
}

scenario("a_mass_hint_without_a_valid_original_version_is_not_drawn_in_the_page", async () => {
  // The block names version 1 -- the released fallback -- for a detail whose own version is
  // missing or invalid. It also carries a released B1.1 hint that must stay drawn.
  const block = H.stepBlock(1, [], {
    ingredients: [ingredientConversion(0, "125 г", [metricTerm], { source_kind: "metric_volume" })],
    mass_conversions: massSection([massVolumeItem(1, { numerator: 1, denominator: 1 })]),
  });

  for (const [label, version] of [
    ["missing", undefined],
    ["null", null],
    ["string", "1"],
    ["zero", 0],
    ["negative", -1],
    ["fractional", 1.5],
    ["unsafe", Number.MAX_SAFE_INTEGER + 2],
    ["boolean", true],
  ]) {
    const page = await openMassCardWithOriginalVersion(version, block);
    assert.equal(massHints(page).length, 0, label);
    assert.equal(massLines(page)[1].textContent, "сахар — 200 г", label + ": stored line");
    // The first line's stored text, beside its own released B1.1 hint.
    assert.equal(
      massLines(page)[0].byClass("recipe-ingredient")[0].textContent,
      "мука — 125 г",
      label + ": first line"
    );
    // B1.1 is exactly as it was: its own hint is still there, from its own section.
    assert.equal(page.el("recipe-body").byClass("measure-hint").length, 1, label);
    assert.ok(page.el("recipe-body").textContent.includes("½ вашего стакана"), label);
  }

  // A valid original version still draws the mass hint beside the untouched B1.1 line.
  const valid = await openMassCardWithOriginalVersion(1, block);
  assert.equal(massHints(valid).length, 1);
  assert.equal(massHints(valid)[0].textContent, "≈ 1 стакан");
  assert.equal(massLines(valid)[1].byClass("recipe-ingredient")[0].textContent, "сахар — 200 г");
  assert.equal(massLines(valid)[1].byClass("recipe-mass-hint")[0].textContent, "≈ 1 стакан");
  assert.equal(massLines(valid)[0].byClass("recipe-mass-hint").length, 0);
  // The line's released B1.1 hint is still there, beside the B2 number.
  assert.equal(massLines(valid)[0].byClass("measure-hint").length, 1, "B1.1 untouched");
  assert.ok(
    massLines(valid)[0].byClass("measure-hint")[0].textContent.includes("½ вашего стакана"),
    "B1.1 untouched"
  );
});

scenario("a_zero_mass_estimate_draws_no_number_and_a_valid_sibling_stays", async () => {
  const page = await openMassCard(
    massSection([
      massVolumeItem(1, { numerator: 1, denominator: 1 }, {}, { mass_g: { numerator: 0, denominator: 1 } }),
      massVolumeItem(0, { numerator: 1, denominator: 1 }),
    ])
  );

  assert.equal(massHints(page).length, 1);
  assert.equal(massHints(page)[0].textContent, "≈ 1 стакан");
  assert.equal(massLines(page)[1].byClass("recipe-mass-hint").length, 0);
  assert.equal(massLines(page)[1].textContent, "сахар — 200 г");
});

scenario("reopening_the_card_draws_the_mass_hint_once", async () => {
  const page = await openMassCard(massSection([massVolumeItem(1, { numerator: 1, denominator: 1 })]));
  assert.equal(massHints(page).length, 1);

  // The same favorite, opened again from the list: the card is replaced, not appended to.
  page.el("favorite-list").byTag("button")[0].click();
  await page.settle();
  await page.settle();
  assert.equal(massHints(page).length, 1);
});

module.exports = { scenarios, useShell, useNoLoginShell };
