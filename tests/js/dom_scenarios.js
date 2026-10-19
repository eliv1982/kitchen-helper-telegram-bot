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

async function open(configure = (env) => env.signedIn([]), { narrow = false, shell = shellHtml } = {}) {
  const env = H.makeEnv();
  configure(env);
  const doc = parseShell(shell);
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

scenario("the_application_lists_conversations_and_shows_the_latest_with_who_said_what", async () => {
  const page = await open(signedInWith([conversation(CONV_B, 30), conversation(CONV_A, 0)], {
    [CONV_B]: historyBody(CONV_B, [message(5, "user", "Про суп"), message(6, "assistant", "Суп так.\n\nИ так.")]),
  }));

  assert.equal(page.visible("screen-app"), true);
  assert.equal(page.visible("chat"), true);
  assert.equal(page.visible("gate"), false);
  const items = page.el("conversation-list").byTag("button");
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
  assert.equal(narrow.doc.activeElement, narrow.el("new-conversation"));
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
      return json(202, { detail: "link_requested", message: "Если этот адрес есть в списке разрешённых, мы отправили письмо." });
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
  assert.equal(page.el("email-result").textContent, "Если этот адрес есть в списке разрешённых, мы отправили письмо.");
  assert.ok(page.el("email-result").className.includes("notice-ok"));
  assert.equal(page.el("email-submit").getAttribute("aria-disabled"), "false");
  assert.equal(page.el("email-submit").textContent, "Отправить ссылку ещё раз");
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
  assert.deepEqual([...names].sort().filter((n) => !["aria-busy", "aria-current", "aria-disabled", "aria-hidden", "aria-invalid", "aria-expanded", "data-drawer", "data-id", "datetime", "type"].includes(n)), []);
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
  for (const name of used("item")) {
    assert.ok(itemKeys.has(name) || listKeys.has(name), "item." + name);
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

module.exports = { scenarios, useShell };
