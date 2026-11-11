"use strict";
/*
 * Scenarios for chat-logic.js against a scripted server (tests/js/harness.js). Each is
 * an async function that asserts; run_scenarios.js runs them and tests/test_web_ui_logic.py
 * reports each as a test. The server's answers are written in the shapes of the real API
 * (tests/test_web_ui.py and tests/test_web_ui_integration.py check the real API against the
 * same shapes), including the answers of a proxy that is not the application.
 */

const assert = require("node:assert/strict");
const H = require("./harness.js");

const { Logic, CONV_A, CONV_B, CONV_C, START, flush, makeEnv, json, text, fail, hang, dropBody } = H;
const { conversation, listBody, message, historyBody, turnBody, apiError, busy, me, notAuthenticated } = H;
const { Storage, USER_A, USER_B } = H;

const scenarios = {};
function scenario(name, fn) {
  assert.ok(!scenarios[name], "duplicate scenario " + name);
  scenarios[name] = fn;
}

const MESSAGES_A = new RegExp("/api/conversations/" + CONV_A + "/messages(\\?|$)");
const URL_A = `/api/conversations/${CONV_A}/messages`;

// A signed-in person with one conversation that has one exchange in it, booted.
async function withConversation(options = {}) {
  const env = makeEnv(options).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]),
  });
  await env.boot();
  return env;
}

const last = (array) => array[array.length - 1];
const timeline = (env) => env.view().thread.items;
const userTexts = (env) => timeline(env).filter((i) => i.role === "user").map((i) => i.text);
const pendingItem = (env) => timeline(env).find((i) => i.type === "pending");
const gets = (env, pattern) => env.net.requests("GET", pattern).length;

function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

// ============================================================================================
// Starting: who is signed in, and what "not signed in" and "cannot tell" look like
// ============================================================================================

scenario("boot_opens_the_most_recent_conversation", async () => {
  const env = makeEnv().signedIn([conversation(CONV_B, 30), conversation(CONV_A, 0)], {
    [CONV_B]: historyBody(CONV_B, [message(5, "user", "Про суп"), message(6, "assistant", "Суп так")]),
  });
  const view = await env.boot();

  assert.equal(view.screen, "app");
  assert.equal(view.currentId, CONV_B);
  assert.deepEqual(view.thread.items.map((i) => i.text), ["Про суп", "Суп так"]);
  assert.deepEqual(env.net.log.map((r) => `${r.method} ${r.url}`), [
    "GET /auth/me",
    "GET /api/conversations?limit=50",
    "GET /api/favorites",
    "GET /api/profile",
    `GET /api/conversations/${CONV_B}/messages?limit=50`,
  ]);
  assert.equal(view.list.items.length, 2);
  assert.equal(view.list.items[0].current, true);
});

scenario("boot_without_conversations_is_a_draft_and_creates_nothing", async () => {
  const env = makeEnv().signedIn([]);
  const view = await env.boot();

  assert.equal(view.screen, "app");
  assert.equal(view.currentId, null);
  assert.equal(view.thread.empty, true);
  assert.equal(view.chatTitle, "Новый разговор");
  assert.equal(env.net.requests("POST", /.*/).length, 0);
});

scenario("boot_401_is_the_login_screen_and_nothing_more_is_asked", async () => {
  const env = makeEnv();
  env.net.route("GET", "/auth/me", notAuthenticated());
  const view = await env.boot();

  assert.equal(view.screen, "login");
  assert.equal(view.notice, null);
  assert.equal(env.net.log.length, 1);
});

scenario("boot_503_is_unavailable_not_login_and_shows_no_invented_id", async () => {
  const env = makeEnv();
  // Even a body that carries an id: the /auth endpoints have none, and none is shown.
  env.net.route("GET", "/auth/me", json(503, { detail: "temporarily_unavailable", correlation_id: "hh-0000000000000001" }));
  const view = await env.boot();

  assert.equal(view.screen, "unavailable");
  assert.equal(view.failure.correlationId, null);
  assert.equal(view.failure.text, Logic.TEXT.unavailable);
});

scenario("boot_network_error_is_unavailable_and_retry_recovers", async () => {
  const env = makeEnv();
  env.net.route("GET", "/auth/me", fail(), me());
  env.net.route("GET", "/api/conversations", listBody([]));

  let view = await env.boot();
  assert.equal(view.screen, "unavailable");
  assert.equal(view.failure.text, Logic.TEXT.network);

  await env.ctl.start();
  await env.settle();
  assert.equal(env.view().screen, "app");
});

scenario("boot_answers_that_are_not_ours_are_unavailable_never_login", async () => {
  const answers = [
    text(502, "<html><body>502 Bad Gateway</body></html>"),
    text(504, "<html>Gateway Time-out</html>"),
    text(200, "<html><body>captive portal</body></html>"),
    json(200, {}),
    json(500, { error: "boom" }),
    text(403, "<html>Forbidden</html>"),
    text(429, "Too Many Requests"),
    text(404, "not found"),
  ];
  for (const answer of answers) {
    const env = makeEnv();
    env.net.route("GET", "/auth/me", answer);
    const view = await env.boot();
    assert.equal(view.screen, "unavailable", JSON.stringify(answer));
  }
});

scenario("boot_login_unavailable_is_the_setup_screen_not_a_failure", async () => {
  const env = makeEnv();
  // What the server answers when no way of signing in is connected (house_helper/web/auth.py): a code, no id.
  env.net.route("GET", "/auth/me", json(503, { detail: "login_unavailable" }));
  const view = await env.boot();

  assert.equal(view.screen, "setup");
  assert.equal(view.failure, null, "nothing failed: nothing to retry or to number");
  assert.equal(view.notice, null);
  assert.equal(view.list.items.length, 0);
  assert.equal(env.net.log.length, 1, "nothing else is asked, and nothing is retried by itself");
  await env.timers.advance(120000);
  assert.equal(env.net.log.length, 1);
  assert.equal(env.view().screen, "setup");
});

scenario("boot_login_unavailable_is_only_the_applications_own_503", async () => {
  const answers = [
    // the other 503s of the application, and of its proxy: failures
    json(503, { detail: "service_unavailable", correlation_id: "hh-0000000000000001" }),
    json(503, { detail: "temporarily_unavailable" }),
    json(503, {}),
    text(503, "<html>Service Unavailable</html>"),
    text(503, "login_unavailable"),
    text(503, "<html><body>login_unavailable</body></html>"),
    json(503, { detail: ["login_unavailable"] }),
    json(503, { detail: "LOGIN_UNAVAILABLE" }),
    json(503, { message: "login_unavailable" }),
    // the same code under any other status: not the application saying it
    json(500, { detail: "login_unavailable" }),
    json(502, { detail: "login_unavailable" }),
    json(504, { detail: "login_unavailable" }),
    json(403, { detail: "login_unavailable" }),
    json(404, { detail: "login_unavailable" }),
    json(200, { detail: "login_unavailable" }),
    fail(),
  ];
  for (const answer of answers) {
    const env = makeEnv();
    env.net.route("GET", "/auth/me", answer);
    const view = await env.boot();
    assert.equal(view.screen, "unavailable", JSON.stringify(answer));
    assert.ok(view.failure && /[А-Яа-я]/.test(view.failure.text), JSON.stringify(answer));
    assert.equal(view.failure.retry, "start");
  }
});

scenario("setup_screen_is_not_a_dead_end_when_login_is_connected_later", async () => {
  const env = makeEnv();
  env.net.route("GET", "/auth/me", json(503, { detail: "login_unavailable" }), notAuthenticated());
  assert.equal((await env.boot()).screen, "setup");

  await env.ctl.recover(); // the page was reopened or the button of another screen pressed: it asks from the beginning
  await env.settle();
  assert.equal(env.view().screen, "login", "now a way of signing in exists, and nobody is signed in");
  assert.equal(env.view().notice, null);
});

scenario("login_unavailable_never_opens_the_application_or_touches_a_signed_in_page", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, OLD_EXCHANGE) });
  await env.boot();
  assert.equal(env.view().screen, "app");

  // The same question, answered "no login here" while somebody's conversations are open: not a setup page, and the
  // conversations are held back like for any answer that cannot say who is signed in.
  env.net.route("GET", "/auth/me", json(503, { detail: "login_unavailable" }));
  env.ctl.recheck();
  await env.settle();
  const view = env.view();
  assert.equal(view.screen, "unverified");
  assert.equal(view.failure.retry, "verify");
  assert.ok(view.failure.text.startsWith(Logic.TEXT.unverified) && !view.failure.text.includes("undefined"), view.failure.text);
  assert.equal(view.thread.items.length, 0);
  assert.equal(view.list.items.length, 0);
});

scenario("classify_me_tells_the_setup_state_from_every_other_answer", async () => {
  const now = Date.UTC(2026, 9, 24, 12, 0, 0);
  const ok = Logic.classifyMe(
    { kind: "http", status: 200, jsonOk: true, json: { user_id: "11111111-1111-4111-8111-111111111111", session_expires_at: "2026-10-25T12:00:00Z" } },
    now
  );
  assert.deepEqual([ok.type, ok.userId], ["ok", "11111111-1111-4111-8111-111111111111"], "a signed-in answer is read exactly as before");
  assert.equal(Logic.classifyMe({ kind: "http", status: 401, jsonOk: true, json: { detail: "not_authenticated" } }, now).type, "auth");
  assert.equal(Logic.classifyMe({ kind: "http", status: 503, jsonOk: true, json: { detail: "login_unavailable" } }, now).type, "setup");
  assert.equal(Logic.classifyMe({ kind: "http", status: 503, jsonOk: true, json: { detail: "temporarily_unavailable" } }, now).type, "unavailable");
  assert.equal(Logic.classifyMe({ kind: "http", status: 503, jsonOk: false, json: null }, now).type, "unavailable");
  assert.equal(Logic.classifyMe({ kind: "network", timedOut: false }, now).type, "unavailable");
});

scenario("boot_timeout_is_unavailable", async () => {
  const env = makeEnv();
  env.net.route("GET", "/auth/me", hang());
  const started = env.ctl.start();
  await flush();
  await env.timers.advance(20000);
  await started;

  assert.equal(env.view().screen, "unavailable");
  assert.equal(env.view().failure.text, Logic.TEXT.timeout);
  assert.equal(env.net.log[0].signal.aborted, true);
});

scenario("list_failure_shows_the_servers_words_and_its_correlation_id", async () => {
  const env = makeEnv();
  env.net.route("GET", "/auth/me", me());
  const failure = apiError(503, "service_unavailable", { retryable: true });
  env.net.route("GET", "/api/conversations", failure);
  const view = await env.boot();

  assert.equal(view.screen, "app");
  assert.equal(view.list.status, "error");
  assert.equal(view.list.error.text, Logic.CODE_MESSAGES.service_unavailable);
  assert.match(view.list.error.correlationId, /^hh-[0-9a-f]{16}$/);
  assert.equal(view.list.error.correlationId, failure.headers["X-Correlation-ID"]);
});

scenario("a_correlation_id_that_is_not_the_servers_shape_is_not_shown", async () => {
  const env = makeEnv();
  env.net.route("GET", "/auth/me", me());
  env.net.route("GET", "/api/conversations", json(503, { detail: "service_unavailable", message: "x", retryable: true, correlation_id: "<script>" }));
  const view = await env.boot();

  assert.equal(view.list.status, "error");
  assert.equal(view.list.error.correlationId, null);
  assert.equal(view.list.error.text, Logic.TEXT.unavailable);
});

// ============================================================================================
// Conversations
// ============================================================================================

scenario("time_a_conversation_without_a_title_is_named_by_its_date_and_nothing_is_saved", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 30), conversation(CONV_B, 0, "Борщ")]);
  const view = await env.boot();

  const zone = process.env.TZ || "UTC";
  const clock = { UTC: "10:30", "Europe/Moscow": "13:30", "America/New_York": "06:30", "Asia/Kolkata": "16:00" }[zone];
  assert.equal(view.list.items[0].label, `Разговор от 18 октября, ${clock}`); // 10:30 UTC: 2h before START (12:00) + 30 min
  assert.equal(view.list.items[1].label, "Борщ");
  assert.ok(env.net.log.every((r) => r.method === "GET"), "naming must not write anything");
});

scenario("a_new_conversation_is_created_lazily_by_the_first_message", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)]);
  await env.boot();
  await env.ctl.startDraft();
  assert.equal(env.view().currentId, null);
  assert.equal(env.net.requests("POST", /.*/).length, 0);

  env.net.route("POST", "/api/conversations", json(201, conversation(CONV_C, 60)));
  env.net.route("POST", `/api/conversations/${CONV_C}/messages`, turnBody(CONV_C, 1, "Что из яиц?", 2, "Омлет."));
  const sent = await env.ctl.send("Что из яиц?");
  await sent.done;
  await env.settle();

  assert.equal(sent.ok, true);
  const posts = env.net.requests("POST", /.*/);
  assert.deepEqual(posts.map((r) => r.url), ["/api/conversations", `/api/conversations/${CONV_C}/messages`]);
  assert.deepEqual(posts[0].body, {});
  assert.equal(posts[0].headers["idempotency-key"], undefined, "creating a conversation has no key");
  const view = env.view();
  assert.equal(view.currentId, CONV_C);
  assert.equal(view.list.items[0].current, true);
  assert.equal(view.list.items.length, 2);
  assert.deepEqual(view.thread.items.map((i) => i.text), ["Что из яиц?", "Омлет."]);
});

scenario("a_refused_creation_keeps_the_text_and_is_not_repeated", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.net.route("POST", "/api/conversations", apiError(403, "forbidden_origin"));
  const sent = await env.ctl.send("Привет");

  assert.equal(sent.ok, false);
  assert.equal(sent.reason, "rejected");
  assert.equal(env.view().composer.error, Logic.CODE_MESSAGES.forbidden_origin);
  assert.equal(env.view().composer.locked, false);
  assert.equal(env.net.requests("POST", "/api/conversations").length, 1);
  assert.equal(env.view().currentId, null);
});

scenario("an_unclear_creation_refreshes_the_list_and_is_never_repeated_blindly", async () => {
  const unclear = [
    text(502, "<html>Bad Gateway</html>"),
    text(504, "<html>Gateway Time-out</html>"),
    fail(),
    text(200, "<html>captive portal</html>"),
    json(201, {}),
    apiError(503, "service_unavailable", { retryable: true }),
    apiError(500, "internal_error", { retryable: true }),
  ];
  for (const answer of unclear) {
    const env = makeEnv().signedIn([]);
    await env.boot();
    env.net.route("POST", "/api/conversations", answer);
    const listBefore = gets(env, "/api/conversations");
    const sent = await env.ctl.send("Привет");
    await env.settle();

    assert.equal(sent.ok, false, JSON.stringify(answer));
    assert.equal(sent.reason, "uncertain");
    assert.equal(env.net.requests("POST", "/api/conversations").length, 1, "never repeated on its own");
    assert.equal(gets(env, "/api/conversations"), listBefore + 1, "the list is read again");
    assert.equal(env.view().banner.text, Logic.TEXT.createUncertain);
    assert.equal(env.view().composer.locked, false);
    assert.ok(env.view().composer.error);
    assert.equal(env.view().currentId, null);

    // The person decides: sending again is their act, and creates (once more) then.
    env.net.route("POST", "/api/conversations", json(201, conversation(CONV_C, 5)));
    env.net.route("POST", `/api/conversations/${CONV_C}/messages`, turnBody(CONV_C, 1, "Привет", 2, "Здравствуйте!"));
    const again = await env.ctl.send("Привет");
    await again.done;
    assert.equal(env.net.requests("POST", "/api/conversations").length, 2);
  }
});

scenario("a_conversation_made_for_a_message_that_was_refused_is_reused", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.net.route("POST", "/api/conversations", json(201, conversation(CONV_C, 5)));
  env.net.route("POST", `/api/conversations/${CONV_C}/messages`, apiError(400, "invalid_message_text"), turnBody(CONV_C, 1, "Привет", 2, "Здравствуйте!"));
  const first = await env.ctl.send("Привет");
  await first.done;

  assert.equal(env.view().currentId, CONV_C);
  assert.equal(pendingItem(env).phase, "rejected");
  const taken = env.ctl.discard(CONV_C);
  assert.equal(taken, "Привет");

  const again = await env.ctl.send(taken);
  await again.done;
  assert.equal(env.net.requests("POST", "/api/conversations").length, 1, "no second conversation");
  assert.equal(env.view().thread.items.length, 2);
});

scenario("select_a_conversation_and_continue_it", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 30), conversation(CONV_B, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "А"), message(2, "assistant", "а")]),
    [CONV_B]: historyBody(CONV_B, [message(3, "user", "Б"), message(4, "assistant", "б")]),
  });
  await env.boot();
  await env.ctl.selectConversation(CONV_B);
  await env.settle();
  assert.equal(env.view().currentId, CONV_B);
  assert.deepEqual(userTexts(env), ["Б"]);

  env.net.route("POST", `/api/conversations/${CONV_B}/messages`, turnBody(CONV_B, 5, "Ещё", 6, "Ещё ответ"));
  const sent = await env.ctl.send("Ещё");
  await sent.done;
  await env.settle();

  assert.equal(env.posts(CONV_B).length, 1);
  assert.equal(env.posts(CONV_A).length, 0);
  assert.deepEqual(env.view().list.items.map((i) => i.id), [CONV_B, CONV_A], "the conversation moves to the top");
  assert.deepEqual(userTexts(env), ["Б", "Ещё"]);
});

// ============================================================================================
// Paging through a history
// ============================================================================================

const range = (from, to, role = (i) => (i % 2 ? "user" : "assistant")) =>
  Array.from({ length: to - from + 1 }, (_, k) => message(from + k, role(from + k), "m" + (from + k)));

scenario("older_pages_are_prepended_without_duplicates", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, range(51, 100), { hasMore: true, nextBefore: 51 }),
  });
  await env.boot();
  assert.equal(env.view().thread.hasMore, true);
  assert.equal(env.net.log.at(-1).url, `${URL_A}?limit=50`);

  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, range(1, 51)));
  await env.ctl.loadOlder();
  await env.settle();

  assert.equal(env.net.log.at(-1).url, `${URL_A}?limit=50&before=51`);
  const ids = timeline(env).map((i) => i.id);
  assert.deepEqual(ids, range(1, 100).map((m) => m.id));
  assert.equal(new Set(ids).size, 100);
  assert.equal(env.view().thread.hasMore, false);
  const before = env.net.log.length;
  await env.ctl.loadOlder();
  assert.equal(env.net.log.length, before, "nothing older: nothing is asked");
});

scenario("refreshing_the_newest_page_keeps_where_the_older_ones_stopped", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, range(51, 100), { hasMore: true, nextBefore: 51 }),
  });
  await env.boot();
  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, range(26, 50), { hasMore: true, nextBefore: 26 }));
  await env.ctl.loadOlder();
  await env.settle();
  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, range(51, 100), { hasMore: true, nextBefore: 51 }));
  await env.ctl.reloadThread();
  await env.settle();

  assert.equal(timeline(env).length, 75);
  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, range(1, 25)));
  await env.ctl.loadOlder();
  await env.settle();
  assert.equal(env.net.log.at(-1).url, `${URL_A}?limit=50&before=26`, "it goes on from where it was, not from the newest page");
  assert.equal(timeline(env).length, 100);
});

scenario("more_news_than_a_page_replaces_the_cache_instead_of_leaving_a_hole", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, range(1, 10)) });
  await env.boot();
  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, range(61, 110), { hasMore: true, nextBefore: 61 }));
  await env.ctl.reloadThread();
  await env.settle();

  assert.equal(timeline(env)[0].id, 61);
  assert.equal(env.view().thread.hasMore, true);
});

scenario("a_failed_older_page_keeps_the_messages_and_can_be_asked_again", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, range(51, 100), { hasMore: true, nextBefore: 51 }),
  });
  await env.boot();
  env.net.route("GET", MESSAGES_A, text(502, "<html>Bad Gateway</html>"), historyBody(CONV_A, range(1, 51)));
  await env.ctl.loadOlder();
  await env.settle();
  assert.equal(timeline(env).length, 50);
  assert.equal(env.view().thread.olderError.text, Logic.TEXT.unavailable);
  assert.equal(env.view().thread.hasMore, true);

  await env.ctl.loadOlder();
  await env.settle();
  assert.equal(timeline(env).length, 100);
  assert.equal(env.view().thread.olderError, null);
});

// ============================================================================================
// Sending: the key, the text, the limit
// ============================================================================================

scenario("a_message_is_sent_trimmed_with_a_printable_ascii_key_and_nothing_unusual", async () => {
  const env = await withConversation();
  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Что приготовить?", 4, "Суп."));
  const sent = await env.ctl.send("   Что приготовить?  \n");
  await sent.done;

  const request = env.posts(CONV_A)[0];
  assert.deepEqual(request.body, { text: "Что приготовить?" });
  assert.match(request.headers["idempotency-key"], /^[!-~]{1,128}$/);
  assert.equal(request.headers["content-type"], "application/json");
  assert.equal(request.init.credentials, "same-origin");
  assert.equal("mode" in request.init, false, "the fetch mode is left to the browser");
  assert.equal("referrerPolicy" in request.init, false);
  assert.equal("origin" in request.headers, false, "Origin is the browser's to send");
  assert.equal(Object.keys(request.headers).some((n) => /^(user|cookie|authorization|x-user)/.test(n)), false, "no user id, cookie or token of ours");
});

scenario("every_message_gets_its_own_key_and_the_generator_is_the_cryptographic_one", async () => {
  const env = await withConversation();
  env.net.route("POST", URL_A, (request) => turnBody(CONV_A, 10 + env.posts(CONV_A).length * 2, "q", 11 + env.posts(CONV_A).length * 2, "a"));
  for (let i = 0; i < 40; i++) {
    const sent = await env.ctl.send("вопрос " + i);
    await sent.done;
  }
  const keys = env.keys(CONV_A);

  assert.equal(keys.length, 40);
  assert.equal(new Set(keys).size, 40);
  assert.ok(keys.every((k) => Logic.isValidIdempotencyKey(k)));
  assert.equal(env.crypto.calls, 40);
});

scenario("the_limit_counts_code_points_not_utf16_units", async () => {
  const cases = [
    ["😀".repeat(2000), true], // 4000 UTF-16 units, 2000 code points
    ["😀".repeat(2001), false],
    ["я".repeat(2000), true],
    ["я".repeat(2001), false],
    ["я".repeat(1999) + "😀", true],
    ["я".repeat(2000) + "😀", false],
    [" ".repeat(3000) + "a" + "\n".repeat(500), true], // trimmed first, as the server does
    ["á".repeat(1000), true], // combining marks are code points too: 2000
    ["á".repeat(1001), false],
  ];
  for (const [value, accepted] of cases) {
    const env = await withConversation();
    env.net.route("POST", URL_A, turnBody(CONV_A, 3, "x", 4, "y"));
    const sent = await env.ctl.send(value);
    if (sent.ok) await sent.done;
    assert.equal(sent.ok, accepted, `${[...value.trim()].length} code points, ${value.length} units`);
    assert.equal(env.posts(CONV_A).length, accepted ? 1 : 0);
    if (!accepted) {
      assert.match(env.view().composer.error, /максимум 2000 символов/);
      assert.equal(sent.reason, "too_long");
    }
  }
  const prepared = Logic.prepareMessage("😀".repeat(2001));
  assert.equal(prepared.count, 2001);
  assert.equal("😀".repeat(2001).length, 4002);
});

scenario("the_limit_is_the_servers_when_the_page_is_told_another", async () => {
  const env = makeEnv({ maxLength: 10 }).signedIn([]);
  await env.boot();
  assert.equal((await env.ctl.send("a".repeat(11))).reason, "too_long");
  assert.equal(env.view().maxLength, 10);
});

scenario("unusable_input_is_refused_without_asking_the_server", async () => {
  const inputs = [
    ["", "empty"], ["   ", "empty"], ["\n\t \r", "empty"], ["　 ", "empty"], ["\u001c\u001f", "empty"], ["\u0085", "empty"],
    ["a\u0000b", "characters"], ["a\u0007b", "characters"], ["a\u007fb", "characters"], ["a\u0085b", "characters"], ["a\u009fb", "characters"],
    ["\ud83d", "characters"], ["x\ude00", "characters"], ["a\ud83db", "characters"],
  ];
  for (const [value, reason] of inputs) {
    const env = await withConversation();
    const sent = await env.ctl.send(value);
    assert.equal(sent.ok, false, JSON.stringify(value));
    assert.equal(sent.reason, reason, JSON.stringify(value));
    assert.equal(env.posts(CONV_A).length, 0);
    assert.ok(env.view().composer.error);
    env.ctl.clearComposerError();
    assert.equal(env.view().composer.error, null);
  }
  // What the server strips at the ends, and tab/newline/carriage return inside, are fine.
  const env = await withConversation();
  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "x", 4, "y"));
  const ok = await env.ctl.send("\u0085 a\tb\r\nc \u001f");
  assert.equal(ok.ok, true);
  assert.equal(env.posts(CONV_A)[0].body.text, "a\tb\r\nc");
});

scenario("without_a_secure_random_source_nothing_is_sent", async () => {
  for (const crypto of [null, {}, { getRandomValues: "nope" }]) {
    const env = makeEnv({ crypto }).signedIn([conversation(CONV_A, 0)]);
    await env.boot();
    const sent = await env.ctl.send("Привет");
    assert.equal(sent.ok, false);
    assert.equal(sent.reason, "no_crypto");
    assert.equal(env.view().composer.error, Logic.TEXT.noCrypto);
    assert.equal(env.posts(CONV_A).length, 0);
  }
});

scenario("pressing_send_twice_sends_once", async () => {
  const env = await withConversation();
  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "x", 4, "y"));
  const [first, second] = await Promise.all([env.ctl.send("x"), env.ctl.send("x")]);

  assert.equal(first.ok, true);
  assert.deepEqual([second.ok, second.reason], [false, "busy"]);
  await first.done;
  assert.equal(env.posts(CONV_A).length, 1);

  const draft = makeEnv().signedIn([]);
  await draft.boot();
  draft.net.route("POST", "/api/conversations", json(201, conversation(CONV_C, 1)));
  draft.net.route("POST", `/api/conversations/${CONV_C}/messages`, turnBody(CONV_C, 1, "x", 2, "y"));
  const both = await Promise.all([draft.ctl.send("x"), draft.ctl.send("x")]);
  assert.deepEqual(both.map((r) => r.ok).sort(), [false, true]);
  await draft.settle();
  assert.equal(draft.net.requests("POST", "/api/conversations").length, 1);
  assert.equal(draft.posts(CONV_C).length, 1);
});

scenario("a_reply_is_merged_by_id_and_everything_is_cleaned_up", async () => {
  const env = await withConversation();
  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Привет", 4, "Здравствуйте!"));
  const sent = await env.ctl.send("Привет");
  await sent.done;

  assert.deepEqual(timeline(env).map((i) => [i.id, i.role, i.text]), [
    [1, "user", "Старый вопрос"], [2, "assistant", "Старый ответ"], [3, "user", "Привет"], [4, "assistant", "Здравствуйте!"],
  ]);
  assert.equal(pendingItem(env), undefined);
  assert.equal(env.storage.record, null);
  assert.ok(env.announced.includes(Logic.TEXT.replied));
  assert.equal(env.view().composer.locked, false);
});

scenario("the_record_of_a_turn_is_written_before_the_request_and_holds_only_what_recovery_needs", async () => {
  const env = await withConversation();
  env.net.route("POST", URL_A, hang());
  const sent = await env.ctl.send("Как варить рис?");
  await flush();

  const record = env.storage.record;
  assert.equal(record.v, 1);
  assert.equal(record.turns.length, 1);
  const turn = record.turns[0];
  assert.deepEqual(Object.keys(turn).sort(), ["at", "b", "c", "k", "t", "u"]);
  assert.equal(turn.u, USER_A, "whose it is: only that person's page ever restores it");
  assert.equal(turn.c, CONV_A);
  assert.equal(turn.k, env.keys(CONV_A)[0]);
  assert.equal(turn.t, "Как варить рис?");
  assert.equal(turn.b, 2);
  assert.equal(turn.at, env.timers.now);
  const stored = JSON.stringify(env.storage.data);
  for (const forbidden of ["cookie", "token", "session", "Старый вопрос", "Старый ответ"]) {
    assert.ok(!stored.includes(forbidden), forbidden);
  }
  await env.timers.advance(90000);
  await sent.done;
});

scenario("only_one_message_at_a_time_in_a_conversation_but_another_conversation_is_free", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 30), conversation(CONV_B, 0)]);
  await env.boot();
  env.net.route("POST", URL_A, hang());
  const first = await env.ctl.send("первый");
  const second = await env.ctl.send("второй");
  assert.equal(second.ok, false);
  assert.equal(second.reason, "busy");

  await env.ctl.selectConversation(CONV_B);
  env.net.route("POST", `/api/conversations/${CONV_B}/messages`, turnBody(CONV_B, 7, "в другом", 8, "ответ"));
  const other = await env.ctl.send("в другом");
  assert.equal(other.ok, true);
  await other.done;
  assert.equal(env.posts(CONV_B).length, 1);
  assert.equal(env.posts(CONV_A).length, 1);
  await env.timers.advance(90000);
  await first.done;
});

// ============================================================================================
// An unclear outcome: never "failed", never a fresh key, never a duplicate
// ============================================================================================

async function uncertainEnv(...postAnswers) {
  const env = await withConversation();
  env.net.route("POST", URL_A, ...postAnswers);
  return env;
}

scenario("a_network_error_keeps_the_key_and_the_text_and_retry_uses_both", async () => {
  const env = await uncertainEnv(fail(), turnBody(CONV_A, 3, "Новый вопрос", 4, "Новый ответ"));
  const sent = await env.ctl.send("Новый вопрос");
  await sent.done;
  await env.settle();

  const item = pendingItem(env);
  assert.equal(item.phase, "uncertain");
  assert.deepEqual(item.actions, ["retry"]);
  assert.equal(item.note, Logic.TEXT.uncertainNetwork);
  assert.equal(env.posts(CONV_A).length, 1);
  assert.equal(timeline(env).length, 3, "the old exchange and the unresolved message, once");
  assert.equal(gets(env, MESSAGES_A), 2, "the history was read to see what the server has");
  assert.equal(env.view().composer.locked, true);

  await env.ctl.retry(CONV_A);
  await env.settle();
  const [first, second] = env.posts(CONV_A);
  assert.equal(second.headers["idempotency-key"], first.headers["idempotency-key"]);
  assert.deepEqual(second.body, first.body);
  assert.equal(env.crypto.calls, 1, "no fresh key was ever made");
  assert.deepEqual(timeline(env).map((i) => i.id), [1, 2, 3, 4]);
  assert.equal(pendingItem(env), undefined);
  assert.equal(env.storage.record, null);
});

scenario("a_proxys_502_504_or_foreign_answer_is_uncertain_never_failed", async () => {
  const answers = [
    text(502, "<html><body>502 Bad Gateway</body></html>"),
    text(504, "<html>Gateway Time-out</html>"),
    json(502, { error: "bad gateway" }),
    json(504, { message: "upstream timed out" }),
    text(500, "Internal Server Error"),
    text(429, "Too Many Requests"),
    text(403, "<html>Forbidden by WAF</html>"),
    text(503, ""),
    json(502, { detail: "provider_unreachable", message: "x", retryable: true, correlation_id: "not-a-real-id" }),
    json(502, { detail: "provider_unreachable", message: "x", retryable: true }),
  ];
  for (const answer of answers) {
    const env = await uncertainEnv(answer);
    const sent = await env.ctl.send("Вопрос");
    await sent.done;
    await env.settle();
    const item = pendingItem(env);
    assert.equal(item.phase, "uncertain", JSON.stringify(answer));
    assert.equal(item.note, Logic.TEXT.uncertainProxy);
    assert.equal(item.correlationId, null);
    assert.deepEqual(item.actions, ["retry"]);
  }
});

scenario("a_timeout_aborts_the_request_and_is_uncertain", async () => {
  const env = await uncertainEnv(hang());
  const sent = await env.ctl.send("Вопрос");
  await flush();
  assert.equal(pendingItem(env).phase, "sending");
  await env.timers.advance(89000);
  assert.equal(pendingItem(env).phase, "sending", "a reply may take up to a minute and a half");
  await env.timers.advance(1500);
  await sent.done;
  await env.settle();

  assert.equal(env.posts(CONV_A)[0].signal.aborted, true);
  const item = pendingItem(env);
  assert.equal(item.phase, "uncertain");
  assert.equal(item.note, Logic.TEXT.uncertainTimeout);
});

scenario("a_body_cut_off_after_the_status_is_uncertain_even_for_a_201", async () => {
  const env = await uncertainEnv(dropBody(201));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();
  assert.equal(pendingItem(env).phase, "uncertain");
});

scenario("a_success_status_with_an_unusable_body_is_uncertain", async () => {
  const good = turnBody(CONV_A, 3, "Вопрос", 4, "Ответ").body;
  const bodies = [
    text(201, "<html>ok</html>"),
    json(201, {}),
    json(201, { ...good, conversation_id: CONV_B }),
    json(201, { ...good, assistant_message: { ...good.assistant_message, id: 2 } }),
    json(201, { ...good, user_message: { ...good.user_message, role: "assistant" } }),
    json(200, { ...good, assistant_message: null }),
  ];
  for (const body of bodies) {
    const env = await uncertainEnv(body);
    const sent = await env.ctl.send("Вопрос");
    await sent.done;
    await env.settle();
    assert.equal(pendingItem(env).phase, "uncertain", JSON.stringify(body));
  }
});

scenario("after_a_502_the_finished_exchange_is_shown_once_and_the_same_key_confirms_it", async () => {
  const env = await uncertainEnv(text(502, "<html>Bad Gateway</html>"), turnBody(CONV_A, 3, "Вопрос", 4, "Ответ", true));
  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, [
    message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"),
    message(3, "user", "Вопрос"), message(4, "assistant", "Ответ"),
  ]));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();

  // The exchange is in the history once, and the message is not settled by looking: it says what it looks like.
  assert.deepEqual(timeline(env).filter((i) => i.type === "message").map((i) => i.id), [1, 2, 3, 4]);
  assert.deepEqual([pendingItem(env).phase, pendingItem(env).note], ["uncertain", Logic.TEXT.uncertainSeen]);
  assert.equal(env.posts(CONV_A).length, 1, "nothing was sent again");
  assert.equal(env.view().composer.locked, true);

  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.equal(pendingItem(env), undefined);
  assert.deepEqual(timeline(env).map((i) => i.id), [1, 2, 3, 4]);
  assert.equal(env.storage.record, null);
  assert.equal(env.keys(CONV_A)[0], env.keys(CONV_A)[1], "the same key: a replay, not a second run");
  assert.equal(env.view().composer.locked, false);
});

scenario("after_a_502_an_unfinished_message_is_adopted_and_followed_by_reading_only", async () => {
  const env = await uncertainEnv(text(502, "<html>Bad Gateway</html>"));
  env.net.route("GET", MESSAGES_A,
    (request) => historyBody(CONV_A, [
      message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"),
      message(3, "user", "Вопрос", { status: "in_progress", idempotency_key: env.keys(CONV_A)[0] }),
    ]),
    (request) => historyBody(CONV_A, [
      message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"),
      message(3, "user", "Вопрос"), message(4, "assistant", "Ответ"),
    ]));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();

  assert.equal(pendingItem(env), undefined, "the history is in charge now");
  assert.equal(env.storage.record, null);
  const unfinished = last(timeline(env));
  assert.deepEqual([unfinished.id, unfinished.status, unfinished.note], [3, "in_progress", Logic.TEXT.preparing]);
  assert.equal(env.view().composer.locked, true);
  assert.ok(env.timers.pending > 0, "the page keeps looking");

  await env.timers.advance(2000);
  await env.settle();
  assert.deepEqual(timeline(env).map((i) => i.id), [1, 2, 3, 4]);
  assert.equal(env.view().composer.locked, false);
  assert.equal(env.timers.pending, 0);
  assert.equal(env.posts(CONV_A).length, 1, "looking never sends");
});

scenario("after_a_502_a_failed_message_is_retried_with_its_own_key_and_text", async () => {
  const env = await uncertainEnv(text(502, "<html>Bad Gateway</html>"), turnBody(CONV_A, 3, "Вопрос", 4, "Ответ"));
  env.net.route("GET", MESSAGES_A, (request) => historyBody(CONV_A, [
    message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"),
    message(3, "user", "Вопрос", { status: "failed", idempotency_key: env.keys(CONV_A)[0], error_code: "provider_error" }),
  ]));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();

  assert.equal(pendingItem(env), undefined);
  const failed = last(timeline(env));
  assert.deepEqual([failed.status, failed.canRetry, failed.note], ["failed", true, Logic.CODE_MESSAGES.provider_error]);

  await env.ctl.retryMessage(CONV_A);
  await env.settle();
  const [first, second] = env.posts(CONV_A);
  assert.equal(second.headers["idempotency-key"], first.headers["idempotency-key"]);
  assert.deepEqual(second.body, { text: "Вопрос" });
  assert.deepEqual(timeline(env).map((i) => i.id), [1, 2, 3, 4]);
});

scenario("only_the_last_failed_message_can_be_retried_and_an_unknown_code_is_a_generic_message", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [
      message(1, "user", "Первый", { status: "failed", idempotency_key: "k-1", error_code: "weird_code" }),
      message(2, "user", "Второй", { status: "failed", idempotency_key: "k-2", error_code: "provider_timeout" }),
    ]),
  });
  await env.boot();
  const [first, second] = timeline(env);

  assert.deepEqual([first.canRetry, first.note], [false, Logic.TEXT.failedGeneric]);
  assert.deepEqual([second.canRetry, second.note], [true, Logic.CODE_MESSAGES.provider_timeout]);
});

scenario("an_older_identical_question_does_not_resolve_a_new_one", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Как варить борщ?"), message(2, "assistant", "Так.")]),
  });
  await env.boot();
  env.net.route("POST", URL_A, fail());
  const sent = await env.ctl.send("Как варить борщ?");
  await sent.done;
  await env.settle();

  assert.equal(pendingItem(env).phase, "uncertain", "the old exchange is not this message's");
  assert.equal(timeline(env).length, 3);
});

scenario("a_retry_after_a_completion_that_could_not_be_seen_replays_without_duplicates", async () => {
  const env = await uncertainEnv(fail(), turnBody(CONV_A, 3, "Вопрос", 4, "Ответ", true));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();
  assert.equal(pendingItem(env).phase, "uncertain", "a stale read shows nothing");

  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.deepEqual(timeline(env).map((i) => i.id), [1, 2, 3, 4]);
  assert.equal(pendingItem(env), undefined);
  assert.equal(env.keys(CONV_A)[0], env.keys(CONV_A)[1]);
});

scenario("a_new_key_is_made_only_for_a_new_message", async () => {
  const env = await uncertainEnv(
    fail(), apiError(502, "provider_error", { retryable: true }), turnBody(CONV_A, 3, "первый", 4, "а"),
    turnBody(CONV_A, 5, "второй", 6, "б")
  );
  const first = await env.ctl.send("первый");
  await first.done;
  await env.settle();
  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.equal(pendingItem(env).phase, "uncertain", "a 5xx is not a verdict: the same key goes on");
  await env.ctl.retry(CONV_A);
  await env.settle();
  const second = await env.ctl.send("второй");
  await second.done;

  const keys = env.keys(CONV_A);
  assert.equal(keys.length, 4);
  assert.equal(new Set(keys).size, 2, "two messages, two keys, however many tries");
  assert.deepEqual([keys[0] === keys[1], keys[1] === keys[2], keys[2] === keys[3]], [true, true, false]);
  assert.equal(env.crypto.calls, 2);
});

scenario("the_network_coming_back_reads_what_the_server_has_of_an_unclear_message", async () => {
  const env = await uncertainEnv(fail());
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();
  const before = gets(env, MESSAGES_A);
  assert.equal(pendingItem(env).note, Logic.TEXT.uncertainNetwork);

  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, [
    message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"),
    message(3, "user", "Вопрос"), message(4, "assistant", "Ответ"),
  ]));
  env.ctl.recheck();
  await env.settle();

  assert.equal(gets(env, MESSAGES_A), before + 1);
  assert.equal(pendingItem(env).note, Logic.TEXT.uncertainSeen, "what it read is shown; nothing is decided by it");
  assert.equal(env.posts(CONV_A).length, 1);

  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, [
    message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"),
    message(3, "user", "Вопрос", { status: "in_progress", idempotency_key: env.keys(CONV_A)[0] }),
  ]));
  env.ctl.recheck(); // the one thing a read does settle: the server has the message under this very key
  await env.settle();
  assert.equal(pendingItem(env), undefined);
  assert.equal(env.storage.record, null);
});

// ============================================================================================
// What the application says
// ============================================================================================

scenario("a_busy_conversation_waits_as_long_as_the_server_says_and_sends_the_same_key_again", async () => {
  const env = await uncertainEnv(busy("conversation_busy"), turnBody(CONV_A, 3, "Вопрос", 4, "Ответ"));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;

  let item = pendingItem(env);
  assert.deepEqual([item.phase, item.note, item.busy], ["waiting", Logic.TEXT.waitingBusy, true]);
  assert.deepEqual(item.actions, []);
  assert.deepEqual(env.timers.delays, [5000]);
  assert.equal(env.posts(CONV_A).length, 1);

  await env.timers.advance(4999);
  assert.equal(env.posts(CONV_A).length, 1);
  await env.timers.advance(1);
  await env.settle();
  assert.equal(env.posts(CONV_A).length, 2);
  assert.equal(env.keys(CONV_A)[0], env.keys(CONV_A)[1]);
  assert.equal(pendingItem(env), undefined);
  assert.deepEqual(timeline(env).map((i) => i.id), [1, 2, 3, 4]);
});

scenario("retry_after_is_taken_from_the_header_or_the_body_and_kept_within_bounds", async () => {
  const cases = [
    [apiError(409, "conversation_busy", { retryable: true, retry_after_seconds: 12 }), 12000],
    [apiError(409, "conversation_busy", { retryable: true, retry_after_seconds: 0 }), 1000],
    [apiError(409, "conversation_busy", { retryable: true, retry_after_seconds: 9999 }), 30000],
    [apiError(409, "turn_in_progress", { retryable: true }), 5000],
    [json(409, { ...apiError(409, "conversation_busy", { retryable: true }).body, retry_after_seconds: 7 }), 7000],
  ];
  for (const [answer, expected] of cases) {
    const env = await uncertainEnv(answer);
    const sent = await env.ctl.send("Вопрос");
    await sent.done;
    assert.deepEqual(env.timers.delays, [expected], JSON.stringify(answer.body));
    await env.timers.advance(expected);
  }
});

scenario("a_turn_already_in_progress_is_waited_for_and_then_replayed", async () => {
  const env = await uncertainEnv(busy("turn_in_progress"), busy("turn_in_progress"), turnBody(CONV_A, 3, "Вопрос", 4, "Ответ", true));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  assert.equal(pendingItem(env).note, Logic.TEXT.waitingOwn);
  await env.timers.advance(5000);
  await env.timers.advance(5000);
  await env.settle();

  assert.equal(env.posts(CONV_A).length, 3);
  assert.equal(new Set(env.keys(CONV_A)).size, 1);
  assert.deepEqual(timeline(env).map((i) => i.id), [1, 2, 3, 4]);
});

scenario("waiting_gives_up_after_many_rounds_and_asks_the_person", async () => {
  const env = await uncertainEnv(busy("conversation_busy"));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  for (let i = 0; i < 40; i++) {
    await env.timers.advance(5000);
  }
  await env.settle();

  const item = pendingItem(env);
  assert.deepEqual([item.phase, item.note, item.actions], ["uncertain", Logic.TEXT.stalled, ["retry"]]);
  assert.equal(env.posts(CONV_A).length, 24);
  assert.equal(env.timers.pending, 0);
  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.equal(env.posts(CONV_A).length, 25);
});

scenario("a_superseded_or_unusable_key_is_offered_as_a_new_message_on_click_only", async () => {
  for (const code of ["turn_superseded", "idempotency_key_unusable", "idempotency_key_reused"]) {
    const env = await uncertainEnv(apiError(409, code), turnBody(CONV_A, 5, "Вопрос", 6, "Ответ"));
    const sent = await env.ctl.send("Вопрос");
    await sent.done;
    await env.settle();

    const item = pendingItem(env);
    assert.deepEqual([item.phase, item.actions, item.note], ["rejected", ["asnew", "edit"], Logic.CODE_MESSAGES[code]], code);
    assert.equal(env.timers.pending, 0, "nothing is sent on its own");
    await env.ctl.retry(CONV_A);
    assert.equal(env.posts(CONV_A).length, 1, "the same key is not offered again");

    await env.ctl.sendAsNew(CONV_A);
    await env.settle();
    const [first, second] = env.posts(CONV_A);
    assert.notEqual(second.headers["idempotency-key"], first.headers["idempotency-key"]);
    assert.deepEqual(second.body, first.body);
    assert.equal(env.crypto.calls, 2);
    assert.equal(pendingItem(env), undefined);
  }
});

scenario("an_application_5xx_is_uncertain_keeps_its_key_and_its_correlation_id_and_offers_only_the_same_key", async () => {
  const failure = apiError(502, "provider_unreachable", { retryable: true, user_message_id: 3 });
  const second = apiError(504, "provider_timeout", { retryable: true });
  const env = await uncertainEnv(failure, second, turnBody(CONV_A, 3, "Вопрос", 4, "Ответ"));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();

  let item = pendingItem(env);
  assert.deepEqual([item.phase, item.actions], ["uncertain", ["retry"]], "an HTTP error is not a verdict on the turn");
  assert.equal(item.note, Logic.TEXT.uncertainServer);
  assert.equal(item.correlationId, failure.headers["X-Correlation-ID"], "kept for diagnostics");
  assert.equal(env.ctl._state().pending[CONV_A].failure.code, "provider_unreachable");
  assert.equal(env.posts(CONV_A).length, 1, "nothing is sent again on its own");
  assert.equal(env.ctl.discard(CONV_A), null);
  assert.equal(env.storage.record.turns[0].t, "Вопрос");

  await env.ctl.retry(CONV_A);
  await env.settle();
  item = pendingItem(env);
  assert.deepEqual([item.phase, item.actions], ["uncertain", ["retry"]]);
  assert.equal(item.correlationId, second.headers["X-Correlation-ID"], "the latest request's number");

  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.equal(new Set(env.keys(CONV_A)).size, 1);
  assert.equal(env.crypto.calls, 1);
  assert.equal(pendingItem(env), undefined);
});

scenario("a_rate_limited_provider_waits_for_the_person_not_the_clock", async () => {
  const env = await uncertainEnv(apiError(503, "provider_rate_limited", { retryable: true, retry_after_seconds: 30 }));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;

  await env.settle();
  const item = pendingItem(env);
  assert.deepEqual([item.phase, item.actions], ["uncertain", ["retry"]]);
  assert.equal(env.timers.pending, 0, "the clock is not what retries it");
});

scenario("an_unavailable_assistant_is_no_reason_to_take_the_text_back_or_make_a_new_key", async () => {
  const env = await uncertainEnv(apiError(503, "chat_unavailable"), turnBody(CONV_A, 3, "Вопрос", 4, "Ответ"));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();

  const item = pendingItem(env);
  assert.deepEqual([item.phase, item.actions, item.note], ["uncertain", ["retry"], Logic.TEXT.uncertainServer]);
  assert.equal(env.ctl.discard(CONV_A), null);
  assert.deepEqual((await env.ctl.send("Вопрос")).reason, "busy");
  assert.equal(env.storage.record.turns.length, 1);

  await env.ctl.retry(CONV_A); // when the assistant is there again, the same message goes on
  await env.settle();
  assert.equal(pendingItem(env), undefined);
  assert.equal(new Set(env.keys(CONV_A)).size, 1);
});

scenario("invalid_input_answers_are_certain_rejections_with_the_servers_words", async () => {
  const codes = [
    [400, "invalid_request"], [400, "invalid_message_text"], [400, "message_too_long"], [413, "request_too_large"],
    [415, "unsupported_media_type"], [400, "empty_message"], [403, "forbidden_origin"], [400, "invalid_idempotency_key"],
    [400, "idempotency_key_required"], [400, "invalid_query"],
  ];
  for (const [status, code] of codes) {
    const answer = apiError(status, code, { message: "Слова сервера для " + code });
    const env = await uncertainEnv(answer);
    const sent = await env.ctl.send("Вопрос");
    await sent.done;
    const item = pendingItem(env);
    assert.deepEqual([item.phase, item.actions, item.note], ["rejected", ["edit"], "Слова сервера для " + code], code);
    assert.equal(item.correlationId, answer.headers["X-Correlation-ID"]);
    assert.equal(env.timers.pending, 0);
  }
});

scenario("an_unknown_conversation_on_send_is_dropped_with_a_plain_message", async () => {
  const env = await uncertainEnv(apiError(404, "conversation_not_found"));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;

  const view = env.view();
  assert.equal(view.currentId, null);
  assert.equal(view.list.items.length, 0);
  assert.equal(view.banner.text, Logic.TEXT.conversationGone);
  assert.equal(env.storage.record, null);
});

scenario("a_401_on_send_ends_the_session_wipes_the_page_and_keeps_the_record_for_the_next_sign_in", async () => {
  const env = await uncertainEnv(notAuthenticated());
  const sent = await env.ctl.send("Вопрос, который нельзя терять");
  await sent.done;
  await env.settle();

  let view = env.view();
  assert.equal(view.screen, "login");
  assert.equal(view.notice.text, Logic.TEXT.authExpired);
  assert.equal(view.list.items.length, 0);
  assert.deepEqual(Object.keys(env.ctl._state().threads), []);
  assert.deepEqual(Object.keys(env.ctl._state().pending), []);
  assert.equal(env.timers.pending, 0);
  assert.equal(env.storage.record.turns.length, 1, "the turn is kept to be resolved after signing in again");

  // Signing in again (the page is loaded again): the turn comes back, with its exact text and key.
  env.net.route("GET", "/auth/me", me());
  await env.boot();
  view = env.view();
  assert.equal(view.screen, "app");
  assert.equal(view.currentId, CONV_A);
  const item = pendingItem(env);
  assert.deepEqual([item.phase, item.text, item.note], ["uncertain", "Вопрос, который нельзя терять", Logic.TEXT.uncertainRestored]);

  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Вопрос, который нельзя терять", 4, "Ответ"));
  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.equal(env.keys(CONV_A)[0], env.keys(CONV_A)[1]);
  assert.equal(pendingItem(env), undefined);
});

scenario("a_401_on_any_read_ends_the_session", async () => {
  const listed = makeEnv();
  listed.net.route("GET", "/auth/me", me());
  listed.net.route("GET", "/api/conversations", notAuthenticated());
  assert.equal((await listed.boot()).screen, "login");

  const opened = makeEnv().signedIn([conversation(CONV_A, 0)]);
  opened.net.route("GET", MESSAGES_A, notAuthenticated());
  assert.equal((await opened.boot()).screen, "login");

  const paged = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, range(51, 100), { hasMore: true, nextBefore: 51 }) });
  await paged.boot();
  paged.net.route("GET", MESSAGES_A, notAuthenticated());
  await paged.ctl.loadOlder();
  await paged.settle();
  assert.equal(paged.view().screen, "login");
  assert.equal(paged.view().notice.text, Logic.TEXT.authExpired);
});

scenario("an_answer_that_arrives_after_the_session_ended_changes_nothing", async () => {
  const env = await withConversation();
  const gate = deferred();
  env.net.route("POST", URL_A, async () => {
    await gate.promise;
    return turnBody(CONV_A, 3, "Вопрос", 4, "Ответ");
  });
  const sent = await env.ctl.send("Вопрос");
  await flush();
  env.net.route("POST", "/auth/logout", { kind: "text", status: 204, body: null, headers: {} });
  assert.equal(await env.ctl.logout(), true);
  gate.resolve();
  await sent.done;
  await env.settle();

  const state = env.ctl._state();
  assert.equal(env.view().screen, "login");
  assert.deepEqual([Object.keys(state.threads), Object.keys(state.pending), state.list.items.length], [[], [], 0]);
  assert.equal(env.storage.record, null);
});

// ============================================================================================
// The record in sessionStorage
// ============================================================================================

function recordOf(overrides = {}) {
  return { u: USER_A, c: CONV_A, k: "web-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", t: "Незаконченный вопрос", b: 2, at: START - 5000, ...overrides };
}

function storageWith(...turns) {
  return new Storage({ [Logic.STORAGE_KEY]: JSON.stringify({ v: 1, turns }) });
}

scenario("a_restored_turn_opens_its_conversation_and_shows_its_exact_text", async () => {
  const storage = storageWith(recordOf());
  const env = makeEnv({ storage }).signedIn([conversation(CONV_B, 30), conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]),
  });
  const view = await env.boot();

  assert.equal(view.currentId, CONV_A, "not the newest conversation: the one with the unresolved message");
  const item = pendingItem(env);
  assert.deepEqual([item.phase, item.text, item.note], ["uncertain", "Незаконченный вопрос", Logic.TEXT.uncertainRestored]);
  assert.equal(view.list.items.find((i) => i.id === CONV_A).meta, "ждет решения");

  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Незаконченный вопрос", 4, "Ответ", true));
  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.equal(env.keys(CONV_A)[0], "web-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
  assert.deepEqual(env.posts(CONV_A)[0].body, { text: "Незаконченный вопрос" });
  assert.equal(pendingItem(env), undefined);
  assert.equal(env.storage.record, null);
});

scenario("a_restored_turn_the_server_has_is_handed_over_to_the_history", async () => {
  const inProgress = makeEnv({ storage: storageWith(recordOf()) }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [
      message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"),
      message(3, "user", "Незаконченный вопрос", { status: "in_progress", idempotency_key: "web-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }),
    ]),
  });
  await inProgress.boot();
  assert.equal(pendingItem(inProgress), undefined);
  assert.equal(inProgress.storage.record, null);
  assert.equal(last(timeline(inProgress)).note, Logic.TEXT.preparing);

  const finished = makeEnv({ storage: storageWith(recordOf()) }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [
      message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"),
      message(3, "user", "Незаконченный вопрос"), message(4, "assistant", "Ответ"),
    ]),
  });
  await finished.boot();
  // A finished exchange with the same words carries no key: it is a hint, and the turn waits for its own key.
  assert.deepEqual([pendingItem(finished).phase, pendingItem(finished).note], ["uncertain", Logic.TEXT.uncertainSeen]);
  assert.equal(timeline(finished).filter((i) => i.type === "message").length, 4);
  assert.equal(finished.storage.record.turns.length, 1);
});

scenario("damaged_or_foreign_records_are_ignored", async () => {
  const now = START;
  const raw = (value) => new Storage({ [Logic.STORAGE_KEY]: value });
  const load = (storage) => Logic.createPendingStore(storage, () => now).load(USER_A);

  for (const value of ["not json", "null", "[]", '"x"', '{"v":2,"turns":[]}', '{"v":1,"turns":"x"}', '{"v":1}', ""]) {
    assert.deepEqual(load(raw(value)), [], value);
  }
  const bad = [
    recordOf({ u: USER_B }), recordOf({ u: "" }), recordOf({ u: 5 }), recordOf({ u: "u".repeat(65) }),
    recordOf({ c: "not-a-uuid" }), recordOf({ k: "has space" }), recordOf({ k: "" }), recordOf({ k: "k".repeat(129) }),
    recordOf({ k: "ключ" }), recordOf({ t: "" }), recordOf({ t: 5 }), recordOf({ t: "😀".repeat(2001) }),
    recordOf({ b: -1 }), recordOf({ b: 1.5 }), recordOf({ b: "2" }), recordOf({ at: "now" }), recordOf({ at: START - 7 * 3600 * 1000 }),
    recordOf({ at: START + 3600 * 1000 }), null, 5, "x", {},
  ];
  for (const turn of bad) {
    assert.deepEqual(load(storageWith(turn)), [], JSON.stringify(turn));
  }
  const good = load(storageWith(recordOf({ extra: "ignored", secret: "x" })));
  assert.deepEqual(Object.keys(good[0]).sort(), ["at", "baseline", "conversationId", "key", "owner", "text"]);
  const withoutOwner = recordOf();
  delete withoutOwner.u;
  assert.deepEqual(load(storageWith(withoutOwner)), [], "a record that does not say whose it is belongs to nobody");
  assert.equal(load(storageWith(...Array.from({ length: 20 }, () => recordOf()))).length, 8);
  assert.equal(load(storageWith(recordOf({ t: "😀".repeat(2000) }))).length, 1);

  const env = makeEnv({ storage: raw("{broken") }).signedIn([]);
  assert.equal((await env.boot()).screen, "app");
});

scenario("storage_that_fails_or_is_missing_does_not_break_sending", async () => {
  for (const options of [{ noStorage: true }, { storage: Object.assign(new Storage(), { broken: true }) }]) {
    const env = makeEnv(options).signedIn([conversation(CONV_A, 0)]);
    await env.boot();
    env.net.route("POST", URL_A, turnBody(CONV_A, 1, "Привет", 2, "Здравствуйте"));
    const sent = await env.ctl.send("Привет");
    await sent.done;
    assert.deepEqual(timeline(env).map((i) => i.text), ["Привет", "Здравствуйте"]);
  }
});

scenario("the_record_goes_when_the_turn_resolves_is_discarded_or_the_person_signs_out", async () => {
  const env = await uncertainEnv(fail());
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();
  assert.equal(env.storage.record.turns.length, 1);

  // The person leaves; the server's answer is lost. The record goes with the conversations, at once, and
  // nothing of them comes back (more in the scenarios of the end of the session, below).
  env.net.route("POST", "/auth/logout", fail());
  assert.equal(await env.ctl.logout(), false);
  assert.equal(env.view().screen, "unavailable");
  assert.equal(env.storage.record, null, "leaving removes the record whatever the server answers");
  assert.equal(pendingItem(env), undefined);

  env.net.route("POST", "/auth/logout", { kind: "text", status: 204, body: null, headers: {} });
  assert.equal(await env.ctl.recover(), true, "leaving again is what the button does");
  assert.equal(env.storage.record, null);
  const view = env.view();
  assert.deepEqual([view.screen, view.notice.text, view.list.items.length, view.thread.items.length], ["login", Logic.TEXT.loggedOut, 0, 0]);
  assert.equal(env.timers.pending, 0);

  // Taken back: only a message whose fate the server has settled (here: it refused it).
  const taken = await uncertainEnv(apiError(400, "invalid_message_text"));
  const again = await taken.ctl.send("Вопрос");
  await again.done;
  await taken.settle();
  assert.equal(taken.ctl.discard(CONV_A), "Вопрос");
  assert.equal(taken.storage.record, null);

  // An unclear one cannot be: the record stays.
  const unclear = await uncertainEnv(fail());
  await (await unclear.ctl.send("Вопрос")).done;
  await unclear.settle();
  assert.equal(unclear.ctl.discard(CONV_A), null);
  assert.equal(unclear.storage.record.turns.length, 1);
});

// ============================================================================================
// What is drawn
// ============================================================================================

scenario("the_pending_message_is_shown_once_even_when_the_history_has_it_too", async () => {
  const env = await withConversation();
  env.net.route("POST", URL_A, hang());
  const sent = await env.ctl.send("Вопрос");
  await flush();
  const key = env.keys(CONV_A)[0];
  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, [
    message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ"),
    message(3, "user", "Вопрос", { status: "in_progress", idempotency_key: key }),
  ]));
  await env.ctl.reloadThread();
  await flush(); // (not settle(): the request is deliberately left hanging)

  assert.deepEqual(userTexts(env), ["Старый вопрос", "Вопрос"]);
  assert.equal(timeline(env).filter((i) => i.type === "pending").length, 1);
  await env.timers.advance(90000);
  await sent.done;
});

scenario("the_composer_says_why_it_is_locked", async () => {
  const idle = await withConversation();
  assert.deepEqual([idle.view().composer.locked, idle.view().composer.hint], [false, ""]);

  const sending = await uncertainEnv(hang());
  const sent = await sending.ctl.send("Вопрос");
  await flush();
  assert.deepEqual([sending.view().composer.locked, sending.view().composer.hint], [true, "Дождитесь ответа помощника."]);
  await sending.timers.advance(90000);
  await sent.done;

  const refused = await uncertainEnv(apiError(400, "invalid_message_text"));
  await (await refused.ctl.send("Вопрос")).done;
  assert.deepEqual([refused.view().composer.locked, refused.view().composer.hint], [true, "Сначала решите, что делать с сообщением выше."]);

  const server = await uncertainEnv(apiError(502, "provider_error", { retryable: true }));
  await (await server.ctl.send("Вопрос")).done;
  assert.deepEqual([server.view().composer.locked, server.view().composer.hint], [true, Logic.TEXT.hintUncertain], "a 5xx leaves it unsettled");

  const draft = makeEnv().signedIn([]);
  await draft.boot();
  const gate = deferred();
  draft.net.route("POST", "/api/conversations", async () => {
    await gate.promise;
    return json(201, conversation(CONV_C, 1));
  });
  draft.net.route("POST", `/api/conversations/${CONV_C}/messages`, turnBody(CONV_C, 1, "x", 2, "y"));
  const creating = draft.ctl.send("x");
  await flush();
  assert.deepEqual([draft.view().composer.locked, draft.view().composer.creating], [true, true]);
  await draft.ctl.selectConversation(CONV_C); // the person cannot leave while it is being made
  assert.equal(draft.view().currentId, null);
  gate.resolve();
  await (await creating).done;
  assert.equal(draft.view().currentId, CONV_C);
});

scenario("texts_stay_data_whatever_they_contain", async () => {
  const hostile = '<img src=x onerror=alert(1)>\n\n<b>bold</b> &amp; <script>alert(2)</script>';
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", hostile), message(2, "assistant", hostile)]),
  });
  await env.boot();
  const items = timeline(env);

  assert.deepEqual(items.map((i) => i.text), [hostile, hostile]);
  const keys = new Set();
  JSON.stringify(env.view(), (key, value) => (keys.add(key), value));
  for (const forbidden of ["html", "innerHTML", "outerHTML", "markup"]) {
    assert.ok(!keys.has(forbidden), forbidden);
  }
});

scenario("a_history_shows_each_status_and_an_unknown_or_malformed_page_is_not_trusted", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [
      message(1, "user", "Готово"), message(2, "assistant", "Ответ"),
      message(3, "user", "Не вышло", { status: "failed", idempotency_key: "k-3", error_code: "interrupted" }),
    ]),
  });
  await env.boot();
  const [done, answer, failed] = timeline(env);
  assert.deepEqual([done.status, done.note, answer.note], ["completed", "", ""]);
  assert.deepEqual([failed.status, failed.note, failed.canRetry], ["failed", Logic.CODE_MESSAGES.interrupted, true]);

  const malformed = [
    { conversation_id: CONV_A, messages: [{ id: "1", role: "user", text: "x" }], has_more: false },
    { conversation_id: CONV_A, messages: [message(1, "system", "x")], has_more: false, next_before: null },
    { conversation_id: CONV_B, messages: [], has_more: false, next_before: null },
    { conversation_id: CONV_A, messages: [message(1, "user", "x", { status: "weird" })], has_more: false, next_before: null },
    { conversation_id: CONV_A, messages: "no", has_more: false },
  ];
  for (const body of malformed) {
    const broken = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: json(200, body) });
    await broken.boot();
    assert.equal(broken.view().thread.status, "error", JSON.stringify(body));
    assert.equal(broken.view().thread.items.length, 0);
  }
});

scenario("every_text_the_page_makes_is_russian", async () => {
  const latin = /[A-Za-z]/;
  // Placeholders are filled in; "JSON", the product name "Telegram" and the provider's own
  // name "OpenAI" are the words written in Latin, as everyone writes them.
  const stripPlaceholders = (value) =>
    value.replace(/\{\w+\}/g, "").replace(/JSON|Telegram|OpenAI/g, "");
  const check = (name, value) => {
    if (value && typeof value === "object") {
      for (const [key, nested] of Object.entries(value)) {
        check(`${name}.${key}`, nested);
      }
      return;
    }
    if (typeof value !== "string") {
      return;
    }
    const shown = stripPlaceholders(value);
    if (shown === "") {
      return; // a proper name alone: the identity method's label
    }
    if (!/[A-Za-zА-Яа-яЁё]/.test(shown)) {
      return; // a symbol-only template such as the approximation marker
    }
    assert.ok(/[А-Яа-яЁё]/.test(shown), `${name} has no Russian`);
    assert.ok(!latin.test(shown), `${name} has Latin letters: ${shown}`);
  };
  for (const [name, value] of Object.entries({ ...Logic.TEXT, ...Logic.CODE_MESSAGES })) {
    check(name, value);
  }
  const env = makeEnv().signedIn([conversation(CONV_A, 0)]);
  const view = await env.boot();
  for (const item of view.list.items) {
    assert.ok(!latin.test(item.label + item.meta), item.label);
  }
});

// ============================================================================================
// Looking at a reply that is still being prepared
// ============================================================================================

scenario("looking_stops_after_a_while_and_a_manual_refresh_starts_it_again", async () => {
  const inProgress = historyBody(CONV_A, [message(1, "user", "Вопрос", { status: "in_progress", idempotency_key: "k-1" })]);
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: inProgress });
  await env.boot();
  assert.equal(env.view().composer.locked, true);

  await env.timers.advance(170000);
  await env.settle();
  assert.equal(env.view().thread.stalled, true);
  assert.equal(env.timers.pending, 0);
  const reads = gets(env, MESSAGES_A);
  assert.ok(reads > 5 && reads < 60, "looked now and then, not in a loop: " + reads);
  assert.equal(env.net.requests("POST", /.*/).length, 0);

  await env.ctl.reloadThread();
  await env.settle();
  assert.equal(env.view().thread.stalled, false);
  assert.ok(env.timers.pending > 0);
});

scenario("looking_stops_when_the_person_leaves_the_conversation", async () => {
  const inProgress = historyBody(CONV_A, [message(1, "user", "Вопрос", { status: "in_progress", idempotency_key: "k-1" })]);
  const env = makeEnv().signedIn([conversation(CONV_A, 30), conversation(CONV_B, 0)], { [CONV_A]: inProgress });
  await env.boot();
  await env.ctl.selectConversation(CONV_B);
  await env.settle();
  const before = gets(env, MESSAGES_A);
  await env.timers.advance(30000);
  await env.settle();

  assert.equal(gets(env, MESSAGES_A), before);
  assert.equal(env.timers.pending, 0);
});

// ============================================================================================
// Email sign-in
// ============================================================================================

const SENT = json(202, { detail: "link_requested", message: "Если этот адрес есть в списке разрешенных, мы отправили письмо." });

scenario("the_email_address_is_trimmed_and_nothing_else_the_local_part_keeps_its_case", async () => {
  const env = makeEnv();
  env.net.route("POST", "/auth/email/request", SENT);
  const result = await env.ctl.requestEmailLink("  Cook@Example.COM \n");

  assert.equal(result.ok, true);
  const request = env.net.log[0];
  assert.deepEqual(request.body, { email: "Cook@Example.COM" });
  assert.equal(request.headers["content-type"], "application/json");
  assert.equal(request.init.credentials, "same-origin");
  assert.equal(result.text, "Если этот адрес есть в списке разрешенных, мы отправили письмо.");
});

scenario("the_email_answer_is_the_same_for_every_address", async () => {
  const env = makeEnv();
  env.net.route("POST", "/auth/email/request", SENT);
  const known = await env.ctl.requestEmailLink("elena@example.com");
  const unknown = await env.ctl.requestEmailLink("stranger@example.org");

  assert.deepEqual(known, unknown);
  assert.deepEqual(Object.keys(known).sort(), ["kind", "ok", "text"]);
  const fallback = makeEnv();
  fallback.net.route("POST", "/auth/email/request", json(202, { detail: "link_requested" }));
  assert.equal((await fallback.ctl.requestEmailLink("a@b.c")).text, Logic.TEXT.emailSent);
  assert.ok(!/(не найден|не зарегистрирован|нет в списке|не в списке|неизвестн)/i.test(Logic.TEXT.emailSent));
});

scenario("an_address_that_cannot_be_one_is_refused_before_the_network", async () => {
  const env = makeEnv();
  for (const value of ["", " ", "x", "a@", "@b", "a@@b", "кот@пример.рф", "a b@c.d", "a@b c", "x".repeat(400) + "@y.z", null, 5]) {
    const result = await env.ctl.requestEmailLink(value);
    assert.deepEqual([result.ok, result.kind, result.text], [false, "invalid", Logic.TEXT.invalidEmail], JSON.stringify(value));
  }
  assert.equal(env.net.log.length, 0);
});

scenario("email_failures_are_plain_and_never_carry_a_correlation_id", async () => {
  const answers = [
    json(400, { detail: "invalid_email", message: "Введите корректный адрес электронной почты." }),
    json(400, { detail: "invalid_request", message: "Запрос не удалось разобрать." }),
    json(403, { detail: "forbidden_origin", message: "Запрос отклонен: он пришел не с нашего сайта." }),
    json(503, { detail: "login_unavailable" }),
    json(503, { detail: "temporarily_unavailable", message: "Сервис временно недоступен. Попробуйте еще раз чуть позже." }),
    json(500, { detail: "server_error", message: "Что-то пошло не так. Попробуйте еще раз чуть позже." }),
    text(502, "<html>Bad Gateway</html>"),
    text(200, "<html>portal</html>"),
    fail(),
  ];
  for (const answer of answers) {
    const env = makeEnv();
    env.net.route("POST", "/auth/email/request", answer);
    const result = await env.ctl.requestEmailLink("a@b.co");
    assert.equal(result.ok, false);
    assert.ok(/[А-Яа-я]/.test(result.text), JSON.stringify(answer));
    assert.ok(!/hh-|correlation|Номер обращения/i.test(JSON.stringify(result)));
    assert.equal("correlationId" in result, false);
  }
  const slow = makeEnv();
  slow.net.route("POST", "/auth/email/request", hang());
  const pendingResult = slow.ctl.requestEmailLink("a@b.co");
  await flush();
  await slow.timers.advance(20000);
  assert.deepEqual([(await pendingResult).ok, (await pendingResult).kind], [false, "timeout"]);
});

// ============================================================================================
// Time and names
// ============================================================================================

scenario("time_a_server_timestamp_is_read_as_the_instant_it_names", async () => {
  const base = Date.UTC(2026, 9, 18, 10, 30, 0);
  const cases = [
    ["2026-10-18T10:30:00+00:00", base], ["2026-10-18T10:30:00Z", base], ["2026-10-18T13:30:00+03:00", base],
    ["2026-10-18T05:30:00-05:00", base], ["2026-10-18T05:30:00-0500", base], ["2026-10-18T10:30:00", base],
    ["2026-10-18 10:30:00", base], ["2026-10-18T10:30:00.123456+00:00", base + 123], ["2026-10-18T10:30:00.5Z", base + 500],
  ];
  for (const [value, expected] of cases) {
    assert.equal(Logic.parseTimestamp(value), expected, value);
  }
  for (const value of ["", "yesterday", "2026-13-01T00:00:00Z", "2026-10-32T00:00:00Z", "2026-10-18T25:00:00Z", "2026-10-18", null, undefined, 5, {}]) {
    assert.equal(Logic.parseTimestamp(value), null, String(value));
  }
});

// 11:30 UTC on 18 October 2026 as the clock of each zone shows it (New York is still on summer time).
const EXPECTED_HOUR = { UTC: 11, "Europe/Moscow": 14, "America/New_York": 7, "Asia/Kolkata": 17 };

scenario("time_names_and_moments_are_in_the_viewers_time", async () => {
  const zone = process.env.TZ || "UTC";
  assert.ok(zone in EXPECTED_HOUR, "unexpected zone " + zone);
  const hour = EXPECTED_HOUR[zone];
  const minute = zone === "Asia/Kolkata" ? "00" : "30"; // 17:00 there (a half-hour offset)
  const clock = `${String(hour).padStart(2, "0")}:${minute}`;
  const created = Date.UTC(2026, 9, 18, 11, 30, 0);
  const now = Date.UTC(2026, 9, 18, 12, 0, 0);

  assert.equal(Logic.conversationLabel({ title: null, createdAt: created }, now), `Разговор от 18 октября, ${clock}`);
  assert.equal(Logic.conversationLabel({ title: "  ", createdAt: created }, now), `Разговор от 18 октября, ${clock}`);
  assert.equal(Logic.conversationLabel({ title: "Борщ", createdAt: created }, now), "Борщ");
  assert.equal(Logic.conversationLabel({ title: null, createdAt: null }, now), "Разговор");
  assert.equal(Logic.formatMoment(created, now), clock);
  assert.equal(Logic.formatMoment(created - 86400000, now), `вчера, ${clock}`);
  assert.equal(Logic.formatMoment(created - 3 * 86400000, now), `15 октября, ${clock}`);
  assert.equal(Logic.formatMoment(created - 400 * 86400000, now), `13 сентября 2025, ${clock}`); // another year: the year is named
  assert.equal(Logic.formatMoment(null, now), "");
});

// ============================================================================================
// The audit of R4B: an unclear message keeps its identity, history is read before sending
// ============================================================================================

const OLD_EXCHANGE = [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")];
const answerTo = (words) => "Ответ на: " + words;

// R4A's server in miniature, for the keys: a key it has not seen is a new turn, the same key again is a
// replay of the turn it kept (200). The first `lose` new turns are run but their answers are lost on the
// way back; the first `ack503` are run and COMPLETED (stored) but the answer is the application's own 503
// "service_unavailable" -- the commit's acknowledgement was lost (chat_api.ChatRoute: SQLAlchemyError ->
// 503), `retryable` as the catalogue says. `ran` (key -> {id, text}) is what the server did; pass one in to
// share it between pages.
function serverThatRemembers(env, { lose = 0, ack503 = 0, retryable = true, ran = new Map() } = {}) {
  let lost = 0;
  let acks = 0;
  env.net.route("POST", URL_A, (request) => {
    const key = request.headers["idempotency-key"];
    if (ran.has(key)) {
      const turn = ran.get(key);
      if (acks < ack503) {
        acks++; // (the turn is stored; the acknowledgements of the replays are lost too)
        return apiError(503, "service_unavailable", { retryable });
      }
      return turnBody(CONV_A, turn.id, turn.text, turn.id + 1, answerTo(turn.text), true);
    }
    const turn = { id: 3 + 2 * ran.size, text: request.body.text };
    ran.set(key, turn);
    if (lost < lose) {
      lost++;
      return fail();
    }
    if (acks < ack503) {
      acks++;
      return apiError(503, "service_unavailable", { retryable });
    }
    return turnBody(CONV_A, turn.id, turn.text, turn.id + 1, answerTo(turn.text));
  });
  return ran;
}

// What that server would show as the history.
const historyOf = (ran) =>
  historyBody(CONV_A, [
    ...OLD_EXCHANGE,
    ...[...ran.values()].flatMap((turn) => [message(turn.id, "user", turn.text), message(turn.id + 1, "assistant", answerTo(turn.text))]),
  ]);

scenario("an_unclear_message_cannot_be_taken_back_so_no_new_key_can_duplicate_it", async () => {
  // The history cannot be read, or is a stale one that shows nothing of the message.
  for (const reading of [fail(), historyBody(CONV_A, OLD_EXCHANGE)]) {
    const env = await withConversation();
    const ran = serverThatRemembers(env, { lose: 1 });
    env.net.route("GET", MESSAGES_A, reading);
    const sent = await env.ctl.send("Что на ужин?");
    await sent.done;
    await env.settle();

    assert.equal(ran.size, 1, "the server ran the turn and its answer was lost");
    const item = pendingItem(env);
    assert.equal(item.phase, "uncertain");
    assert.deepEqual(item.actions, ["retry"], "nothing is offered that would forget the key");
    const key = env.keys(CONV_A)[0];

    assert.equal(env.ctl.discard(CONV_A), null, "the text cannot be taken back");
    assert.equal(pendingItem(env).text, "Что на ужин?");
    assert.equal(env.storage.record.turns[0].k, key);
    const again = await env.ctl.send("Что на ужин?");
    assert.deepEqual([again.ok, again.reason], [false, "busy"]);
    assert.equal(env.posts(CONV_A).length, 1, "no second request, no second key");

    await env.ctl.retry(CONV_A);
    await env.settle();
    assert.equal(ran.size, 1, "one logical turn on the server");
    assert.equal(new Set(env.keys(CONV_A)).size, 1);
    assert.equal(env.crypto.calls, 1);
    assert.deepEqual(userTexts(env), ["Старый вопрос", "Что на ужин?"]);
    assert.equal(pendingItem(env), undefined);
    assert.equal(env.storage.record, null);
  }
});

// --- the second audit: an application 5xx is not a verdict -------------------------------------------------

scenario("a_503_after_a_committed_completion_is_one_logical_turn_whatever_is_tried_next", async () => {
  // The turn was completed and stored; only the acknowledgement failed: the server answered 503
  // "service_unavailable". The page used to call that "failed" and offer to take the text back / send it
  // as a new message, which made a second logical turn. It must not, whatever `retryable` says.
  for (const retryable of [true, false]) {
    const env = await withConversation();
    const ran = serverThatRemembers(env, { ack503: 1, retryable });
    env.net.route("GET", MESSAGES_A, () => historyOf(ran)); // what the server really has
    const answer = (await env.ctl.send("Что на ужин?"));
    await answer.done;
    await env.settle();

    assert.equal(ran.size, 1, "the server completed it");
    const first = env.net.log.filter((r) => r.method === "POST").length;
    assert.equal(first, 1);
    let item = pendingItem(env);
    assert.deepEqual([item.phase, item.actions], ["uncertain", ["retry"]], "retryable=" + retryable);
    assert.equal(env.ctl._state().pending[CONV_A].reason, "server");
    assert.match(item.correlationId, /^hh-[0-9a-f]{16}$/, "the number of the request is kept for diagnostics");
    assert.equal(env.ctl._state().pending[CONV_A].failure.code, "service_unavailable");
    assert.equal(item.note, Logic.TEXT.uncertainSeen, "the completed exchange is seen in the history, as a hint only");
    assert.deepEqual(timeline(env).filter((i) => i.type === "message").map((i) => i.id), [1, 2, 3, 4], "the history's exchange, once");

    // Everything that could make a second logical turn, none of it possible:
    assert.equal(env.ctl.discard(CONV_A), null, "no 'edit'");
    await env.ctl.sendAsNew(CONV_A);
    await env.ctl.retryMessage(CONV_A);
    const again = await env.ctl.send("Что на ужин?");
    assert.deepEqual([again.ok, again.reason], [false, "busy"]);
    await env.ctl.selectConversation(CONV_A);
    await env.ctl.reloadThread();
    env.ctl.recheck(); // the tab comes back: the same person, the same unresolved message
    await env.settle();
    assert.equal(env.view().composer.locked, true);
    assert.equal(env.net.log.filter((r) => r.method === "POST").length, 1, "nothing was sent but the first request");
    assert.equal(env.storage.record.turns.length, 1);

    // The same key, as often as the person likes: the server replays.
    await env.ctl.retry(CONV_A);
    await env.settle();
    assert.equal(ran.size, 1, "exactly one logical turn on the server");
    assert.equal(new Set(env.keys(CONV_A)).size, 1, "exactly one key");
    assert.equal(env.crypto.calls, 1);
    assert.equal(pendingItem(env), undefined);
    assert.deepEqual(userTexts(env), ["Старый вопрос", "Что на ужин?"]);
    assert.equal(env.storage.record, null);
    assert.deepEqual(env.net.log.filter((r) => r.method === "POST").length, 2);
  }
});

scenario("a_503_after_a_committed_completion_stays_one_turn_when_the_person_retries_several_times_and_the_history_is_unreadable", async () => {
  const env = await withConversation();
  const ran = serverThatRemembers(env, { ack503: 3 }); // three acknowledgements in a row are lost
  env.net.route("GET", MESSAGES_A, fail());
  await (await env.ctl.send("Что на ужин?")).done;
  await env.settle();
  for (let i = 0; i < 2; i++) {
    assert.equal(pendingItem(env).phase, "uncertain");
    await env.ctl.retry(CONV_A);
    await env.settle();
  }
  assert.equal(pendingItem(env).phase, "uncertain");
  assert.deepEqual(pendingItem(env).actions, ["retry"]);
  await env.ctl.retry(CONV_A); // a fourth time: the server replays
  await env.settle();

  assert.equal(ran.size, 1);
  assert.equal(new Set(env.keys(CONV_A)).size, 1);
  assert.equal(env.keys(CONV_A).length, 4);
  assert.equal(env.crypto.calls, 1);
  assert.equal(pendingItem(env), undefined);
});

scenario("no_5xx_is_a_verdict_whatever_its_code_or_retryable", async () => {
  const answers = [
    [500, "internal_error"], [500, "interrupted"], [502, "provider_unreachable"], [502, "provider_error"],
    [502, "provider_empty_response"], [503, "provider_rate_limited"], [503, "chat_unavailable"],
    [503, "service_unavailable"], [504, "provider_timeout"], [599, "internal_error"],
  ];
  for (const [status, code] of answers) {
    for (const retryable of [true, false]) {
      const answer = apiError(status, code, { retryable, user_message_id: 3 });
      const env = await uncertainEnv(answer);
      await (await env.ctl.send("Вопрос")).done;
      await env.settle();
      const label = `${status} ${code} retryable=${retryable}`;
      const item = pendingItem(env);
      assert.deepEqual([item.phase, item.actions], ["uncertain", ["retry"]], label);
      assert.equal(item.correlationId, answer.headers["X-Correlation-ID"], label);
      assert.equal(env.ctl.discard(CONV_A), null, label);
      assert.equal((await env.ctl.send("Вопрос")).reason, "busy", label);
      assert.equal(env.crypto.calls, 1, label);
      assert.equal(env.timers.pending, 0, label);
      assert.equal(env.storage.record.turns.length, 1, label);
    }
  }
});

scenario("a_5xx_is_known_to_have_failed_only_when_the_history_has_the_message_failed_under_its_key", async () => {
  // The read says nothing (it fails): the 502 is still not a verdict.
  const blind = await withConversation();
  blind.net.route("POST", URL_A, apiError(502, "provider_unreachable", { retryable: true }));
  blind.net.route("GET", MESSAGES_A, fail());
  await (await blind.ctl.send("Вопрос")).done;
  await blind.settle();
  assert.deepEqual([pendingItem(blind).phase, pendingItem(blind).actions], ["uncertain", ["retry"]]);

  // The read shows the server's own record: the message, under the turn's key, "failed". Now it is known, and it
  // is the history's from here on -- its own same-key retry, the number of the failed request kept on it.
  const env = await withConversation();
  const failure = apiError(502, "provider_unreachable", { retryable: true, user_message_id: 3 });
  env.net.route("POST", URL_A, failure, turnBody(CONV_A, 3, "Вопрос", 4, "Ответ", true));
  env.net.route("GET", MESSAGES_A, () =>
    historyBody(CONV_A, [
      ...OLD_EXCHANGE,
      message(3, "user", "Вопрос", { status: "failed", idempotency_key: env.keys(CONV_A)[0], error_code: "provider_unreachable" }),
    ])
  );
  await (await env.ctl.send("Вопрос")).done;
  await env.settle();

  assert.equal(pendingItem(env), undefined, "handed over to the history");
  const failed = last(timeline(env));
  assert.deepEqual([failed.status, failed.canRetry, failed.note], ["failed", true, Logic.CODE_MESSAGES.provider_unreachable]);
  assert.equal(failed.correlationId, failure.headers["X-Correlation-ID"], "the failed request's number stays with the message");
  assert.equal(env.view().composer.locked, false);
  assert.equal(env.storage.record, null);

  await env.ctl.retryMessage(CONV_A);
  await env.settle();
  assert.equal(new Set(env.keys(CONV_A)).size, 1, "the history's retry uses the same key");
  assert.equal(env.crypto.calls, 1);
  assert.deepEqual(userTexts(env), ["Старый вопрос", "Вопрос"]);
});

scenario("an_unresolved_message_survives_changing_conversation_and_reloading_and_text_alone_never_resolves_it", async () => {
  const storage = new Storage();
  const first = makeEnv({ storage }).signedIn([conversation(CONV_A, 30), conversation(CONV_B, 0)], {
    [CONV_A]: historyBody(CONV_A, OLD_EXCHANGE),
    [CONV_B]: historyBody(CONV_B, [message(7, "user", "Про суп"), message(8, "assistant", "Суп так")]),
  });
  await first.boot();
  const ran = serverThatRemembers(first, { lose: 1 });
  await (await first.ctl.send("Что на ужин?")).done;
  await first.settle();
  const key = first.keys(CONV_A)[0];
  const record = JSON.stringify(storage.record);

  // Away and back: still the conversation's, with its key.
  await first.ctl.selectConversation(CONV_B);
  await first.settle();
  assert.equal(pendingItem(first), undefined, "it is shown in its own conversation");
  await first.ctl.selectConversation(CONV_A);
  await first.settle();
  assert.deepEqual([pendingItem(first).phase, pendingItem(first).actions], ["uncertain", ["retry"]]);
  assert.equal(JSON.stringify(storage.record), record);

  // A reload (a new page, the same tab's storage). The server did run it, and the history now shows the
  // finished exchange: that is not proof, only a hint -- the message stays until the same key is sent again.
  const second = makeEnv({ storage }).signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyOf(ran) });
  serverThatRemembers(second, { ran });
  await second.boot();
  const item = pendingItem(second);
  assert.deepEqual([item.phase, item.actions, item.text, item.note], ["uncertain", ["retry"], "Что на ужин?", Logic.TEXT.uncertainSeen]);
  assert.equal(second.ctl.discard(CONV_A), null);
  assert.equal(second.view().composer.locked, true);
  assert.equal(second.posts(CONV_A).length, 0, "reading never sends");
  assert.equal(JSON.stringify(storage.record), record);

  await second.ctl.retry(CONV_A);
  await second.settle();
  assert.equal(second.keys(CONV_A)[0], key);
  assert.equal(ran.size, 1, "the replay, not a second run");
  assert.deepEqual(timeline(second).map((i) => i.id), [1, 2, 3, 4]);
  assert.equal(pendingItem(second), undefined);
  assert.equal(storage.record, null);
});

scenario("a_completed_exchange_with_the_same_words_never_resolves_a_pending_message_by_itself", async () => {
  // A baseline of 0 is what a send made before the history had loaded used to record: whatever the baseline,
  // an exchange with the same words is not this message's proof.
  const env = makeEnv({ storage: storageWith(recordOf({ t: "Повтори", b: 0 })) }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Повтори"), message(2, "assistant", "Старый ответ")]),
  });
  await env.boot();

  const item = pendingItem(env);
  assert.deepEqual([item.phase, item.text], ["uncertain", "Повтори"]);
  assert.equal(env.storage.record.turns.length, 1);
  assert.deepEqual(timeline(env).filter((i) => i.type === "message").map((i) => i.id), [1, 2]);
});

scenario("sending_waits_for_the_history_and_an_older_identical_exchange_cannot_resolve_a_new_message", async () => {
  const older = [message(1, "user", "Повтори"), message(2, "assistant", "Старый ответ")];
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {});
  const gate = deferred();
  env.net.route("GET", MESSAGES_A, async () => {
    await gate.promise;
    return historyBody(CONV_A, older);
  });
  const starting = env.ctl.start(); // the history is on its way
  await flush();
  env.net.route("POST", URL_A, fail()); // and whatever is sent now would not reach the server

  const early = await env.ctl.send("Повтори");
  assert.deepEqual([early.ok, early.reason], [false, "history"]);
  assert.equal(env.posts(CONV_A).length, 0);
  assert.equal(pendingItem(env), undefined);
  const view = env.view();
  assert.deepEqual([view.thread.status, view.composer.locked, view.composer.hint], ["loading", true, Logic.TEXT.historyLoading]);

  gate.resolve();
  await starting;
  await env.settle();
  assert.deepEqual(timeline(env).map((i) => i.id), [1, 2]);
  assert.equal(env.view().composer.locked, false);

  // Now the same words again; this request is lost before the server.
  const sent = await env.ctl.send("Повтори");
  assert.equal(sent.ok, true);
  await sent.done;
  await env.settle();
  assert.equal(pendingItem(env).phase, "uncertain", "not resolved by the old exchange");
  assert.equal(env.storage.record.turns[0].b, 2, "the baseline is what the server had");
  assert.deepEqual(userTexts(env), ["Повтори", "Повтори"]);
  assert.deepEqual(timeline(env).filter((i) => i.type === "message").map((i) => i.id), [1, 2], "the old exchange stays apart");
  assert.equal(env.view().composer.locked, true);
});

scenario("a_conversation_whose_history_did_not_load_is_not_an_empty_one", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)]);
  env.net.route("GET", MESSAGES_A, fail());
  await env.boot();

  let view = env.view();
  assert.deepEqual([view.thread.status, view.thread.empty, view.composer.locked, view.composer.hint], ["error", false, true, Logic.TEXT.historyUnavailable]);
  const refused = await env.ctl.send("Вопрос");
  assert.deepEqual([refused.ok, refused.reason], [false, "history"]);
  assert.equal(env.net.requests("POST", /.*/).length, 0);

  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, OLD_EXCHANGE));
  await env.ctl.reloadThread();
  await env.settle();
  view = env.view();
  assert.deepEqual([view.thread.status, view.composer.locked, view.composer.hint], ["ready", false, ""]);
  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Вопрос", 4, "Ответ"));
  const sent = await env.ctl.send("Вопрос");
  assert.equal(sent.ok, true);
  await sent.done;
});

scenario("a_late_history_of_a_conversation_the_person_left_changes_nothing_here", async () => {
  const env = makeEnv().signedIn([conversation(CONV_B, 30), conversation(CONV_A, 0)], {
    [CONV_B]: historyBody(CONV_B, [message(7, "user", "Про суп"), message(8, "assistant", "Суп так")]),
  });
  await env.boot();
  const gate = deferred();
  env.net.route("GET", MESSAGES_A, async () => {
    await gate.promise;
    return historyBody(CONV_A, OLD_EXCHANGE);
  });
  const opening = env.ctl.selectConversation(CONV_A);
  await flush();
  assert.deepEqual([env.view().currentId, env.view().composer.locked], [CONV_A, true]);
  await env.ctl.selectConversation(CONV_B);
  await flush();

  gate.resolve();
  await opening;
  await env.settle();
  const view = env.view();
  assert.equal(view.currentId, CONV_B);
  assert.deepEqual(view.thread.items.map((i) => i.id), [7, 8]);
  assert.equal(view.composer.locked, false, "this conversation's own history decides");
  assert.deepEqual(env.ctl._state().threads[CONV_A].messages.map((m) => m.id), [1, 2], "kept for the conversation it belongs to");
});

scenario("a_finished_exchange_in_the_history_is_only_a_hint_and_the_same_key_confirms_it", async () => {
  const env = await withConversation();
  const ran = serverThatRemembers(env, { lose: 1 });
  env.net.route("GET", MESSAGES_A, () => historyOf(ran));
  const sent = await env.ctl.send("Вопрос");
  await sent.done;
  await env.settle();

  assert.equal(pendingItem(env).phase, "uncertain");
  assert.equal(pendingItem(env).note, Logic.TEXT.uncertainSeen);
  assert.equal(env.posts(CONV_A).length, 1, "nothing was sent again");
  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.equal(ran.size, 1);
  assert.deepEqual(timeline(env).map((i) => i.id), [1, 2, 3, 4]);
  assert.equal(env.keys(CONV_A)[0], env.keys(CONV_A)[1]);
});

// --- the end of the session -----------------------------------------------------------------------------------

scenario("a_session_that_runs_out_while_the_page_is_open_takes_the_conversation_off_the_page", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, OLD_EXCHANGE) });
  env.net.route("GET", "/auth/me", me(600)); // ends 600 s after the clock's start
  await env.boot();
  env.net.route("POST", URL_A, fail());
  await (await env.ctl.send("Недосланный вопрос")).done;
  await env.settle();
  const asked = env.net.log.length;

  await env.timers.advance(599000);
  assert.equal(env.ctl.checkSession(), true);
  assert.equal(env.view().screen, "app");
  await env.timers.advance(1000);
  assert.equal(env.ctl.checkSession(), false);

  const view = env.view();
  assert.deepEqual([view.screen, view.notice.text], ["login", Logic.TEXT.authExpired]);
  assert.deepEqual([view.list.items.length, view.thread.items.length, view.banner], [0, 0, null]);
  const state = env.ctl._state();
  assert.deepEqual([Object.keys(state.threads), Object.keys(state.pending)], [[], []]);
  assert.equal(env.net.log.length, asked, "the deadline is known: nothing is asked of the server");
  assert.equal(env.storage.record.turns.length, 1, "the unresolved message waits for the next sign-in");

  // Signed in again: the message is back with its key, and there is no way to send it as a new one.
  env.net.route("GET", "/auth/me", me());
  await env.boot();
  const item = pendingItem(env);
  assert.deepEqual([item.phase, item.actions, item.text], ["uncertain", ["retry"], "Недосланный вопрос"]);
  assert.equal(env.view().composer.locked, true);
  assert.equal(env.ctl.discard(CONV_A), null);
  assert.equal((await env.ctl.send("Недосланный вопрос")).ok, false);
  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Недосланный вопрос", 4, "Ответ", true));
  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.equal(env.keys(CONV_A)[0], env.keys(CONV_A)[1]);
  assert.equal(pendingItem(env), undefined);
});

scenario("answers_that_arrive_after_the_deadline_do_not_bring_the_conversation_back", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, OLD_EXCHANGE) });
  env.net.route("GET", "/auth/me", me(60));
  await env.boot();
  const post = deferred();
  const read = deferred();
  env.net.route("POST", URL_A, async () => {
    await post.promise;
    return turnBody(CONV_A, 3, "Вопрос", 4, "Ответ");
  });
  const sent = await env.ctl.send("Вопрос");
  await flush();
  env.net.route("GET", MESSAGES_A, async () => {
    await read.promise;
    return historyBody(CONV_A, [...OLD_EXCHANGE, message(3, "user", "Вопрос"), message(4, "assistant", "Ответ")]);
  });
  const reading = env.ctl.reloadThread();
  const listing = deferred();
  env.net.route("GET", "/api/conversations", async () => {
    await listing.promise;
    return listBody([conversation(CONV_A, 0)]);
  });
  const listed = env.ctl.reloadList();
  await flush();

  await env.timers.advance(61000); // all three are still out when the deadline passes
  env.ctl.checkSession();
  assert.equal(env.view().screen, "login");
  post.resolve();
  read.resolve();
  listing.resolve();
  await sent.done;
  await reading;
  await listed;
  await env.settle();

  const state = env.ctl._state();
  assert.equal(env.view().screen, "login");
  assert.deepEqual([Object.keys(state.threads), Object.keys(state.pending), state.list.items.length], [[], [], 0]);
  assert.equal(env.view().list.items.length, 0, "not the late list either");
  assert.equal(env.storage.record.turns.length, 1, "the answer was not seen, so the message is still unresolved");
});

scenario("a_conversation_being_made_when_the_session_ends_is_not_continued", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/auth/me", me(60));
  await env.boot();
  const creation = deferred();
  env.net.route("POST", "/api/conversations", async () => {
    await creation.promise;
    return json(201, conversation(CONV_C, 1));
  });
  env.net.route("POST", `/api/conversations/${CONV_C}/messages`, turnBody(CONV_C, 1, "Привет", 2, "Здравствуйте"));
  const sending = env.ctl.send("Привет");
  await flush();
  assert.equal(env.view().composer.creating, true);

  await env.timers.advance(61000);
  env.ctl.checkSession();
  creation.resolve();
  const result = await sending;
  await env.settle();

  assert.deepEqual([result.ok, result.reason], [false, "stale"]);
  assert.equal(env.view().screen, "login");
  const state = env.ctl._state();
  assert.deepEqual([Object.keys(state.threads), Object.keys(state.pending), state.list.items.length, state.currentId, state.creating], [[], [], 0, null, false]);
  assert.equal(env.posts(CONV_C).length, 0, "the message was not sent into a conversation of a session that has ended");
  assert.equal(env.storage.record, null);
});

scenario("a_page_that_wakes_up_is_signed_out_before_it_asks_anything_if_the_deadline_passed", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, OLD_EXCHANGE) });
  env.net.route("GET", "/auth/me", me(3600));
  await env.boot();
  const asked = env.net.log.length;

  env.timers.now += 3 * 86400000; // the machine slept: the clock moved and no timer ran
  env.ctl.recheck();
  await env.settle();
  assert.deepEqual([env.view().screen, env.view().notice.text], ["login", Logic.TEXT.authExpired]);
  assert.equal(env.net.log.length, asked);
});

scenario("coming_back_to_the_page_asks_whether_the_session_is_still_there", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, OLD_EXCHANGE) });
  await env.boot();
  const asks = () => env.net.requests("GET", "/auth/me").length;
  const before = asks();

  env.ctl.recheck();
  assert.equal(env.view().screen, "verifying", "held back from the moment it is asked");
  await env.settle();
  assert.deepEqual([asks(), env.view().screen], [before + 1, "app"]);
  assert.equal(env.view().thread.items.length, 2);

  env.net.route("GET", "/auth/me", fail()); // cannot tell: the conversations stay out of sight, nothing is claimed
  env.ctl.recheck();
  await env.settle();
  assert.deepEqual([env.view().screen, env.view().banner, env.view().thread.items.length], ["unverified", null, 0]);
  assert.equal(env.view().failure.retry, "verify");

  env.net.route("GET", "/auth/me", notAuthenticated()); // ended elsewhere (signed out in another tab, revoked)
  await env.ctl.recover();
  await env.settle();
  assert.deepEqual([env.view().screen, env.view().notice.text, env.view().thread.items.length], ["login", Logic.TEXT.authExpired, 0]);
});

// ============================================================================================
// The second audit: who is signed in is a privacy boundary
// ============================================================================================

const ANNA = { question: "Секретный вопрос Анны", answer: "Секретный ответ Анне", unsent: "Недосланное сообщение Анны" };
const NOTHING_OF_ANNA = [ANNA.question, ANNA.answer, ANNA.unsent, CONV_A];

// A signed-in person (USER_A, "Анна") with one conversation, and one message of hers that did not get an answer.
async function annaWithAnUnresolvedMessage(options = {}) {
  const env = makeEnv(options).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", ANNA.question), message(2, "assistant", ANNA.answer)]),
  });
  await env.boot();
  env.net.route("POST", URL_A, fail());
  await (await env.ctl.send(ANNA.unsent)).done;
  await env.settle();
  assert.equal(pendingItem(env).phase, "uncertain");
  return env;
}

// What a script, a screen reader or the next person could read: the view, the announcements, the storage -- and,
// for a page that has started over, the controller's memory as well.
const traces = (env, { memory = false, storage = true } = {}) =>
  JSON.stringify([env.view(), env.announced, storage ? env.storage.data : null, memory ? env.ctl._state() : null]);

function assertNothingOfAnna(env, label, options) {
  const all = traces(env, options);
  for (const words of NOTHING_OF_ANNA) assert.ok(!all.includes(words), `${label}: still there: ${words}`);
}

// Boris's server: the same browser, another session.
function borisIsSignedIn(env, { seconds = 86400 } = {}) {
  env.net.route("GET", "/auth/me", me(seconds, USER_B));
  env.net.route("GET", "/api/conversations", listBody([conversation(CONV_B, 0)]));
  env.net.route(
    "GET",
    new RegExp("/api/conversations/" + CONV_B + "/messages(\\?|$)"),
    historyBody(CONV_B, [message(7, "user", "Про суп Бориса"), message(8, "assistant", "Суп так")])
  );
  env.net.route("GET", MESSAGES_A, apiError(404, "conversation_not_found")); // Anna's is nobody else's
}

scenario("a_different_person_on_coming_back_never_sees_the_last_ones_conversations", async () => {
  const env = await annaWithAnUnresolvedMessage();
  const aboutAnna = env.net.log.length;
  borisIsSignedIn(env);

  env.ctl.recheck(); // the tab comes back; nothing is known yet
  assert.equal(env.view().screen, "verifying");
  assertNothingOfAnna(env, "while asking", { storage: false }); // (her record stays in the tab for her own recovery)
  assert.deepEqual([env.view().list.items.length, env.view().thread.items.length, env.view().banner], [0, 0, null]);
  assert.equal(env.view().composer.locked, true);

  await env.settle();
  const view = env.view();
  assert.equal(view.screen, "app");
  assert.equal(view.currentId, CONV_B);
  assert.deepEqual(view.list.items.map((i) => i.id), [CONV_B]);
  assert.deepEqual(timeline(env).map((i) => i.text), ["Про суп Бориса", "Суп так"]);
  assertNothingOfAnna(env, "after", { memory: true });
  assert.deepEqual(Object.keys(env.ctl._state().pending), [], "her unresolved message is not his");
  assert.equal(env.storage.record, null, "and her record of it is gone from the tab");
  assert.equal(view.banner, null, "nothing is said to him about hers");
  assert.equal(view.composer.locked, false);

  // Nothing was asked of her conversation with his session, and her message was not sent into his.
  const since = env.net.log.slice(aboutAnna);
  assert.deepEqual(since.filter((r) => r.url.includes(CONV_A)), []);
  assert.deepEqual(since.map((r) => `${r.method} ${r.url.split("?")[0]}`), [
    "GET /auth/me", "GET /auth/me", "GET /api/conversations", "GET /api/favorites", "GET /api/profile",
    `GET /api/conversations/${CONV_B}/messages`,
  ]);
});

scenario("the_same_person_on_coming_back_gets_everything_back_as_it_was", async () => {
  const env = await annaWithAnUnresolvedMessage();
  const state = env.ctl._state();
  const [turn, thread] = [state.pending[CONV_A], state.threads[CONV_A]];
  const key = turn.key;
  const record = JSON.stringify(env.storage.record);
  const lists = gets(env, "/api/conversations");

  env.ctl.recheck();
  assert.equal(env.view().screen, "verifying");
  assertNothingOfAnna(env, "while asking", { storage: false }); // (her record stays in the tab for her own recovery)
  await env.settle();

  assert.equal(env.view().screen, "app");
  assert.equal(env.ctl._state().pending[CONV_A], turn, "the very same unresolved message, not a restored copy");
  assert.equal(env.ctl._state().threads[CONV_A], thread, "the conversation was not reloaded from nothing");
  assert.equal(turn.key, key);
  assert.deepEqual([pendingItem(env).phase, pendingItem(env).text, pendingItem(env).actions], ["uncertain", ANNA.unsent, ["retry"]]);
  assert.deepEqual(timeline(env).map((i) => i.text), [ANNA.question, ANNA.answer, ANNA.unsent]);
  assert.equal(JSON.stringify(env.storage.record), record, "the record is as it was");
  assert.equal(gets(env, "/api/conversations"), lists, "not started over: the list is not asked for again");
  assert.equal(env.view().currentId, CONV_A);
  assert.equal(env.view().composer.locked, true);

  env.net.route("POST", URL_A, turnBody(CONV_A, 3, ANNA.unsent, 4, "Ответ", true));
  await env.ctl.retry(CONV_A);
  await env.settle();
  assert.equal(new Set(env.keys(CONV_A)).size, 1, "still one key");
  assert.equal(pendingItem(env), undefined);
});

scenario("while_the_page_is_held_back_nothing_is_asked_for_the_conversations_and_nothing_can_be_done_to_them", async () => {
  const env = await annaWithAnUnresolvedMessage();
  const gate = deferred();
  env.net.route("GET", "/auth/me", async () => {
    await gate.promise;
    return me();
  });
  const asked = env.net.log.length;
  env.ctl.recheck();
  await flush();

  // Every way in, closed.
  assert.deepEqual(await env.ctl.send("что-то"), { ok: false, reason: "not_ready" });
  assert.equal(env.ctl.discard(CONV_A), null);
  await env.ctl.retry(CONV_A);
  await env.ctl.retryMessage(CONV_A);
  await env.ctl.sendAsNew(CONV_A);
  await env.ctl.loadOlder();
  await env.ctl.reloadList();
  await env.ctl.reloadThread();
  assert.equal(await env.ctl.logout(), false, "not from a page that does not know who is there");
  assert.deepEqual(env.net.log.slice(asked).map((r) => `${r.method} ${r.url}`), ["GET /auth/me"]);

  gate.resolve();
  await env.settle();
  assert.equal(env.view().screen, "app");
});

scenario("work_waiting_on_a_timer_does_not_run_while_held_back_and_goes_on_for_the_same_person", async () => {
  const env = await withConversation();
  env.net.route("POST", URL_A, busy(), turnBody(CONV_A, 3, "Вопрос", 4, "Ответ", true));
  const sent = await env.ctl.send("Вопрос");
  await flush();
  assert.equal(pendingItem(env).phase, "waiting");

  const gate = deferred();
  env.net.route("GET", "/auth/me", async () => {
    await gate.promise;
    return me();
  });
  env.ctl.recheck();
  await env.timers.advance(20000); // the five-second wait is over, many times
  assert.equal(env.posts(CONV_A).length, 1, "no request is made for a page nobody has been identified at");

  gate.resolve();
  await sent.done;
  await env.settle();
  assert.equal(env.posts(CONV_A).length, 2);
  assert.equal(new Set(env.keys(CONV_A)).size, 1, "the same key");
  assert.equal(pendingItem(env), undefined);
  assert.deepEqual(userTexts(env), ["Старый вопрос", "Вопрос"]);
});

scenario("when_who_is_signed_in_cannot_be_found_out_the_page_stays_held_back_and_claims_nothing", async () => {
  const answers = [fail(), hang(), json(503, { detail: "temporarily_unavailable" }), text(502, "<html>Bad gateway</html>"),
    json(200, { user_id: USER_A }), json(200, { ok: true })];
  for (const answer of answers) {
    const env = await annaWithAnUnresolvedMessage();
    env.net.route("GET", "/auth/me", answer);
    env.ctl.recheck();
    if (answer.kind === "hang") await env.timers.advance(21000);
    await env.settle();

    const view = env.view();
    assert.equal(view.screen, "unverified", JSON.stringify(answer));
    assert.ok(view.failure.text.startsWith(Logic.TEXT.unverified), "says what is wrong");
    assert.equal(view.failure.retry, "verify");
    assert.equal(view.notice, null, "it does not say anyone is signed in, or out");
    assertNothingOfAnna(env, "unverified", { storage: false });
    assert.equal(env.ctl.checkSession(), true);

    // It is asked again when the person asks (or the network is back); and the answer decides.
    env.net.route("GET", "/auth/me", me());
    await env.ctl.recover();
    await env.settle();
    assert.equal(env.view().screen, "app");
    assert.equal(pendingItem(env).text, ANNA.unsent, "the same person: the unresolved message is still there");
  }

  // ...and the answer can be another person, or none at all.
  const other = await annaWithAnUnresolvedMessage();
  other.net.route("GET", "/auth/me", fail());
  other.ctl.recheck();
  await other.settle();
  borisIsSignedIn(other);
  await other.ctl.recover();
  await other.settle();
  assert.equal(other.view().currentId, CONV_B);
  assertNothingOfAnna(other, "another person after not knowing", { memory: true });

  const over = await annaWithAnUnresolvedMessage();
  over.net.route("GET", "/auth/me", fail());
  over.ctl.recheck();
  await over.settle();
  over.timers.now += 3 * 86400000; // the held-back page is still subject to the clock
  assert.equal(over.ctl.checkSession(), false);
  assert.deepEqual([over.view().screen, over.view().notice.text], ["login", Logic.TEXT.authExpired]);
  assertNothingOfAnna(over, "expired while held back", { memory: true, storage: false }); // (her record waits for her next sign-in, as after any session end)
});

scenario("coming_back_asks_again_each_time_and_a_401_is_the_end_of_the_session", async () => {
  const env = await annaWithAnUnresolvedMessage();
  for (let i = 0; i < 3; i++) {
    env.ctl.recheck();
    env.ctl.recheck(); // (a second event while the first is out is the same question)
    await env.settle();
    assert.equal(env.view().screen, "app");
  }
  assert.equal(env.net.requests("GET", "/auth/me").length, 1 + 3);
  env.net.route("GET", "/auth/me", notAuthenticated());
  env.ctl.recheck();
  await env.settle();
  assert.deepEqual([env.view().screen, env.view().notice.text], ["login", Logic.TEXT.authExpired]);
  assertNothingOfAnna(env, "401", { memory: true, storage: false });
  assert.equal(env.storage.record.turns.length, 1, "her record waits for her next sign-in, as after any 401");
});

scenario("answers_meant_for_the_last_person_change_nothing_after_another_one_has_taken_over", async () => {
  const env = await annaWithAnUnresolvedMessage();
  const post = deferred();
  const read = deferred();
  const listing = deferred();
  env.net.route("POST", URL_A, async () => {
    await post.promise;
    return turnBody(CONV_A, 3, ANNA.unsent, 4, ANNA.answer + " (новый)", true);
  });
  const retrying = env.ctl.retry(CONV_A); // out
  env.net.route("GET", MESSAGES_A, async () => {
    await read.promise;
    return historyBody(CONV_A, [message(1, "user", ANNA.question), message(2, "assistant", ANNA.answer)]);
  });
  const reading = env.ctl.reloadThread(); // out
  env.net.route("GET", "/api/conversations", async () => {
    await listing.promise;
    return listBody([conversation(CONV_A, 0)]);
  });
  const listed = env.ctl.reloadList(); // out
  await flush();

  borisIsSignedIn(env);
  env.ctl.recheck();
  await flush(80); // (not settle(): Anna's three requests are deliberately still out)
  assert.equal(env.view().currentId, CONV_B);
  const before = traces(env, { memory: true });

  // Now Anna's answers arrive. They are for a person who is not there.
  post.resolve();
  read.resolve();
  listing.resolve();
  await retrying;
  await reading;
  await listed;
  await env.settle();
  await env.timers.advance(60000);

  assert.equal(traces(env, { memory: true }), before, "nothing changed");
  assertNothingOfAnna(env, "late", { memory: true });
  assert.deepEqual(Object.keys(env.ctl._state().threads), [CONV_B]);
  assert.deepEqual(Object.keys(env.ctl._state().pending), []);
  assert.equal(env.storage.record, null);
});

scenario("a_record_of_the_last_person_is_never_restored_for_another_one", async () => {
  const boris = { u: USER_B, c: CONV_B, t: "Недосланное Бориса", k: "web-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" };
  const stored = () => storageWith(recordOf({ t: ANNA.unsent }), recordOf(boris), recordOf({ u: USER_A, t: "Просроченное Анны", at: START - 7 * 3600 * 1000, k: "web-cccccccccccccccccccccccccccccccc" }));

  // Boris opens the tab that was Anna's (a reload, or a first load after she has gone).
  const asBoris = makeEnv({ storage: stored() }).signedIn([conversation(CONV_B, 0)], {
    [CONV_B]: historyBody(CONV_B, [message(7, "user", "Про суп Бориса"), message(8, "assistant", "Суп так")]),
  });
  asBoris.net.route("GET", "/auth/me", me(86400, USER_B));
  const view = await asBoris.boot();
  assert.equal(view.currentId, CONV_B);
  assert.deepEqual(Object.keys(asBoris.ctl._state().pending), [CONV_B]);
  assert.equal(pendingItem(asBoris).text, "Недосланное Бориса");
  assert.equal(view.banner, null, "the age of what is not his is not told to him");
  assert.deepEqual(asBoris.storage.record.turns.map((t) => [t.u, t.t]), [[USER_B, "Недосланное Бориса"]], "Anna's are gone from the tab");
  assert.ok(!traces(asBoris, { memory: true }).includes(ANNA.unsent));
  assert.equal(asBoris.net.requests("GET", MESSAGES_A).length, 0, "her conversation is not even asked for");

  // Anna opens it: hers, and only hers.
  const asAnna = makeEnv({ storage: stored() }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", ANNA.question), message(2, "assistant", ANNA.answer)]),
  });
  await asAnna.boot();
  assert.equal(pendingItem(asAnna).text, ANNA.unsent);
  assert.equal(asAnna.view().banner.text, Logic.TEXT.expiredRecords, "her own old one is told to her");
  assert.deepEqual(asAnna.storage.record.turns.map((t) => t.u), [USER_A]);
  assert.ok(!traces(asAnna, { memory: true }).includes("Недосланное Бориса"));

  // A record from before records said whose they were (or without it) is nobody's.
  const legacy = recordOf({ t: "Старая запись" });
  delete legacy.u;
  const old = makeEnv({ storage: storageWith(legacy) }).signedIn([conversation(CONV_A, 0)]);
  await old.boot();
  assert.deepEqual([Object.keys(old.ctl._state().pending), old.storage.record], [[], null]);
});

// --- leaving --------------------------------------------------------------------------------------------------------

// The sign-out names the person it is for (the server's guard: it ends that person's session or none), and the
// answer "409 session_mismatch" is the server saying the browser's session is somebody else's now.
const logoutRequests = (env) => env.net.requests("POST", "/auth/logout");
const guardOf = (request) => request.headers["x-expected-user"];
const sessionMismatch = () => json(409, { detail: "session_mismatch" });

scenario("a_lost_answer_to_logout_never_brings_the_conversation_back_not_even_a_late_reply", async () => {
  const env = await annaWithAnUnresolvedMessage();
  const reply = deferred();
  env.net.route("POST", URL_A, async () => {
    await reply.promise; // the answer to a message that was on its way when she pressed "Выйти"
    return turnBody(CONV_A, 3, ANNA.unsent, 4, ANNA.answer + " (поздний)", true);
  });
  const retrying = env.ctl.retry(CONV_A);
  await flush();
  assert.equal(pendingItem(env).phase, "sending");
  const read = deferred();
  env.net.route("GET", MESSAGES_A, async () => {
    await read.promise;
    return historyBody(CONV_A, [message(1, "user", ANNA.question), message(2, "assistant", ANNA.answer)]);
  });
  const reading = env.ctl.reloadThread();
  await flush();

  // The server revokes the session and commits; the response is lost on the way back.
  let revoked = 0;
  env.net.route("POST", "/auth/logout", () => {
    revoked++;
    return fail();
  });
  const leaving = env.ctl.logout();
  // ...from the moment she asks:
  assert.equal(env.view().screen, "signing_out");
  assertNothingOfAnna(env, "leaving", { memory: true });
  assert.equal(env.storage.record, null);
  assert.ok(env.timers.delays.every((ms) => ms === 90000 || ms === 20000), "only the time limits of requests already out: " + env.timers.delays);
  assert.equal(await leaving, false);
  assert.equal(revoked, 1);
  assert.equal(guardOf(env.net.requests("POST", "/auth/logout")[0]), USER_A, "the request says whose sign-out it is");

  const view = env.view();
  assert.equal(view.screen, "unavailable");
  assert.equal(view.failure.text, Logic.TEXT.logoutUnconfirmed, "says what is not known");
  assert.equal(view.failure.retry, "logout");
  assert.ok(!/Вы вышли/.test(JSON.stringify(view)), "no claim that the session is closed");
  assert.equal(view.notice, null);
  assertNothingOfAnna(env, "unconfirmed", { memory: true });

  // Her late reply, and the late history, arrive.
  reply.resolve();
  read.resolve();
  await retrying;
  await reading;
  await env.settle();
  await env.timers.advance(120000);
  assert.equal(env.view().screen, "unavailable");
  assertNothingOfAnna(env, "late reply", { memory: true });
  assert.ok(!traces(env, { memory: true }).includes("(поздний)"));
  assert.equal(env.announced.includes(Logic.TEXT.replied), false, "no 'the assistant replied' for a page that has been left");
  assert.equal(env.storage.record, null);

  // A tab coming back does not bring it back either; nothing is asked for it.
  const asked = env.net.log.length;
  env.ctl.recheck();
  await env.settle();
  assert.equal(env.net.log.length, asked);
  assert.equal(env.view().screen, "unavailable");

  // The way out: leave again. The server has no session any more (it revoked it the first time): 401 or 204, both mean "closed".
  env.net.route("POST", "/auth/logout", { kind: "text", status: 204, body: null, headers: {} });
  assert.equal(await env.ctl.recover(), true);
  assert.deepEqual([env.view().screen, env.view().notice.text], ["login", Logic.TEXT.loggedOut]);
  assertNothingOfAnna(env, "left", { memory: true });
});

scenario("leaving_that_cannot_be_confirmed_can_be_tried_again_and_a_401_means_there_is_no_session", async () => {
  for (const answer of [fail(), hang(), apiError(503, "service_unavailable"), json(403, { detail: "forbidden_origin" }), text(502, "<html/>")]) {
    const env = await withConversation();
    env.net.route("POST", "/auth/logout", answer);
    const leaving = env.ctl.logout();
    if (answer.kind === "hang") await env.timers.advance(21000);
    assert.equal(await leaving, false, JSON.stringify(answer));
    assert.deepEqual([env.view().screen, env.view().failure.retry], ["unavailable", "logout"]);
    assert.equal(env.view().thread.items.length, 0);
    assert.equal(env.storage.record, null);

    env.net.route("POST", "/auth/logout", fail());
    assert.equal(await env.ctl.recover(), false, "still not known: still no conversations");
    assert.equal(env.view().screen, "unavailable");
    assert.equal(env.net.requests("POST", "/auth/logout").length, 2);

    env.net.route("POST", "/auth/logout", notAuthenticated()); // no session: nothing to close
    assert.equal(await env.ctl.recover(), true);
    assert.deepEqual([env.view().screen, env.view().notice.text], ["login", Logic.TEXT.loggedOut]);
  }

  // Leaving from anywhere but the signed-in page does nothing.
  const out = makeEnv();
  out.net.route("GET", "/auth/me", notAuthenticated());
  await out.boot();
  assert.equal(await out.ctl.logout(), false);
  assert.equal(out.net.requests("POST", "/auth/logout").length, 0);
});

scenario("after_an_unconfirmed_logout_a_new_start_is_a_new_page_not_the_old_one", async () => {
  const env = await annaWithAnUnresolvedMessage();
  env.net.route("POST", "/auth/logout", fail());
  await env.ctl.logout();
  // (The session did survive: the request never reached the server.) The person reloads the page.
  env.net.route("GET", "/api/conversations", listBody([conversation(CONV_A, 0)]));
  env.net.route("GET", MESSAGES_A, historyBody(CONV_A, [message(1, "user", ANNA.question), message(2, "assistant", ANNA.answer)]));
  await env.boot();
  assert.equal(env.view().screen, "app");
  assert.deepEqual(timeline(env).map((i) => i.text), [ANNA.question, ANNA.answer], "what the server has, read afresh");
  assert.equal(pendingItem(env), undefined, "her unresolved message was given up with the explicit logout");
});

scenario("every_try_of_a_sign_out_names_the_person_the_page_was_signed_in_as_and_only_in_the_header", async () => {
  const env = await annaWithAnUnresolvedMessage();
  env.net.route("POST", "/auth/logout", fail());
  assert.equal(await env.ctl.logout(), false);
  // Named although the page has already forgotten her by the time it sends: it was captured first.
  assert.deepEqual(logoutRequests(env).map(guardOf), [USER_A]);
  assert.equal(env.ctl._logoutPrincipal(), USER_A);

  // Boris signs in, in another tab. The page asks nothing about him; every try still names her.
  borisIsSignedIn(env);
  const asked = gets(env, "/auth/me");
  for (let i = 0; i < 3; i++) {
    assert.equal(await env.ctl.recover(), false);
  }
  env.ctl.recheck();
  await env.settle();
  assert.deepEqual(logoutRequests(env).map(guardOf), [USER_A, USER_A, USER_A, USER_A]);
  assert.equal(gets(env, "/auth/me"), asked, "who is signed in is not asked while a sign-out is unconfirmed");
  assert.ok(logoutRequests(env).every((r) => r.url === "/auth/logout" && r.body === undefined), "only the header says it");
  assert.equal(env.ctl._logoutPrincipal(), USER_A);

  env.net.route("POST", "/auth/logout", text(204, null));
  assert.equal(await env.ctl.recover(), true);
  assert.equal(guardOf(last(logoutRequests(env))), USER_A);
  assert.deepEqual([env.view().screen, env.view().notice.text], ["login", Logic.TEXT.loggedOut]);
  assert.equal(env.ctl._logoutPrincipal(), null, "settled: not kept");
  assertNothingOfAnna(env, "left", { memory: true });
});

scenario("a_repeated_sign_out_that_finds_somebody_else_signed_in_stops_and_signs_nobody_out", async () => {
  const env = await annaWithAnUnresolvedMessage();
  // The server ended her session, and the answer was lost.
  env.net.route("POST", "/auth/logout", fail());
  assert.equal(await env.ctl.logout(), false);
  assert.equal(env.view().failure.retry, "logout");

  // Boris signed in, in another tab. Her old page offers to leave again; the server finds his session in the cookie.
  borisIsSignedIn(env);
  env.net.route("POST", "/auth/logout", sessionMismatch());
  const asked = env.net.log.length;
  assert.equal(await env.ctl.recover(), false);

  assert.deepEqual(env.net.log.slice(asked).map((r) => `${r.method} ${r.url}`), ["POST /auth/logout"], "that request and nothing else");
  assert.equal(guardOf(last(logoutRequests(env))), USER_A, "hers, not whoever is signed in now");
  const view = env.view();
  assert.equal(view.screen, "unavailable");
  assert.equal(view.failure.text, Logic.TEXT.logoutMismatch);
  assert.equal(view.failure.retry, "start", "the button begins again; it does not leave again");
  assert.ok(!/Вы вышли/.test(JSON.stringify(view)), "no claim that anything was closed");
  assert.equal(view.notice, null);
  assert.equal(env.ctl._logoutPrincipal(), null, "dropped: the sign-out is over");
  assertNothingOfAnna(env, "mismatch", { memory: true });
  assert.ok(!traces(env, { memory: true }).includes(USER_A) && !traces(env, { memory: true }).includes(USER_B));
  assert.ok(!Logic.TEXT.logoutMismatch.includes(USER_B), "and it says nothing about who");

  // Nothing signs Boris out: not the clock, not the tab coming back, not a second look at the screen.
  env.ctl.recheck();
  await env.settle();
  await env.timers.advance(120000);
  assert.equal(env.view().screen, "unavailable");
  assert.equal(logoutRequests(env).length, 2);

  // The button asks who is signed in, from the beginning; the page is Boris's, with none of hers.
  await env.ctl.recover();
  await env.settle();
  assert.equal(env.view().screen, "app");
  assert.equal(env.view().currentId, CONV_B);
  assert.deepEqual(timeline(env).map((i) => i.text), ["Про суп Бориса", "Суп так"]);
  assertNothingOfAnna(env, "Boris's page", { memory: true });
  assert.equal(logoutRequests(env).length, 2, "still nobody was signed out");

  // His own sign-out names him, not her.
  env.net.route("POST", "/auth/logout", text(204, null));
  assert.equal(await env.ctl.logout(), true);
  assert.equal(guardOf(last(logoutRequests(env))), USER_B);
});

scenario("a_first_sign_out_whose_cookie_became_somebody_elses_is_refused_the_same_way", async () => {
  const env = await annaWithAnUnresolvedMessage();
  borisIsSignedIn(env); // (before her first click reached the server)
  env.net.route("POST", "/auth/logout", sessionMismatch());

  assert.equal(await env.ctl.logout(), false);

  assert.deepEqual(logoutRequests(env).map(guardOf), [USER_A]);
  assert.deepEqual([env.view().screen, env.view().failure.text, env.view().failure.retry], ["unavailable", Logic.TEXT.logoutMismatch, "start"]);
  assert.equal(env.ctl._logoutPrincipal(), null);
  assertNothingOfAnna(env, "refused", { memory: true });
  assert.equal(env.storage.record, null, "her record of the unresolved message went with the sign-out she asked for");
});

scenario("only_the_servers_own_session_mismatch_ends_the_sign_out_anything_else_stays_unconfirmed_with_the_same_person", async () => {
  const others = [
    json(409, { detail: "conversation_busy" }), json(409, { detail: "session_mismatch_not" }), json(409, { detail: "Session_Mismatch" }),
    json(409, { message: "session_mismatch" }), text(409, "session_mismatch"), text(409, "<html>session_mismatch</html>"),
    apiError(409, "conversation_busy"), json(400, { detail: "invalid_request" }), json(403, { detail: "forbidden_origin" }),
    json(403, { detail: "session_mismatch" }), json(500, { detail: "session_mismatch" }), json(503, { detail: "temporarily_unavailable" }),
    text(502, "<html/>"), fail(), hang(),
  ];
  for (const answer of others) {
    const env = await annaWithAnUnresolvedMessage();
    env.net.route("POST", "/auth/logout", answer);
    const leaving = env.ctl.logout();
    if (answer.kind === "hang") await env.timers.advance(21000);
    assert.equal(await leaving, false, JSON.stringify(answer));

    assert.deepEqual([env.view().screen, env.view().failure.text, env.view().failure.retry], ["unavailable", Logic.TEXT.logoutUnconfirmed, "logout"], JSON.stringify(answer));
    assert.equal(env.ctl._logoutPrincipal(), USER_A, JSON.stringify(answer));
    assertNothingOfAnna(env, "unconfirmed", { memory: true });
    env.net.route("POST", "/auth/logout", fail());
    await env.ctl.recover();
    assert.equal(guardOf(last(logoutRequests(env))), USER_A);
  }
});

scenario("after_an_unconfirmed_sign_out_a_new_start_forgets_whom_it_was_for_and_the_principal_is_in_memory_only", async () => {
  const env = await annaWithAnUnresolvedMessage();
  env.net.route("POST", "/auth/logout", fail());
  await env.ctl.logout();
  assert.equal(env.ctl._logoutPrincipal(), USER_A);
  const stored = JSON.stringify(env.storage.data);
  assert.ok(!stored.includes(USER_A), "never written to the storage: " + stored);
  assert.ok(!JSON.stringify(env.view()).includes(USER_A), "nor in anything the page draws");
  assert.ok(!JSON.stringify(env.announced).includes(USER_A));

  // The page begins again (a reload, or the button of another screen) while Boris is signed in.
  borisIsSignedIn(env);
  await env.boot();
  assert.equal(env.view().screen, "app");
  assert.equal(env.ctl._logoutPrincipal(), null);
  assertNothingOfAnna(env, "Boris's page", { memory: true });

  env.net.route("POST", "/auth/logout", text(204, null));
  assert.equal(await env.ctl.logout(), true);
  assert.deepEqual(logoutRequests(env).map(guardOf), [USER_A, USER_B], "his sign-out is his; hers was not carried over to it");
});

scenario("a_start_that_finds_no_session_leaves_nothing_of_an_unconfirmed_sign_out", async () => {
  const env = await annaWithAnUnresolvedMessage();
  env.net.route("POST", "/auth/logout", fail());
  await env.ctl.logout();
  assert.equal(env.ctl._logoutPrincipal(), USER_A);

  // The page begins again (a later visit) and the server says there is no session: a plain 401.
  env.net.route("GET", "/auth/me", notAuthenticated());
  await env.boot();

  assert.equal(env.view().screen, "login");
  assert.equal(env.ctl._logoutPrincipal(), null);
  assert.equal(logoutRequests(env).length, 1, "nothing was sent in anybody's name");
});

scenario("an_answer_that_does_not_say_when_the_session_ends_is_not_taken_for_a_session", async () => {
  const user = "99999999-9999-4999-8999-999999999999";
  const bodies = [
    { user_id: user }, { user_id: user, session_expires_at: null }, { user_id: user, session_expires_at: "" },
    { user_id: user, session_expires_at: "soon" }, { user_id: user, session_expires_at: 1790000000 },
    { user_id: user, session_expires_at: "2026-13-40T00:00:00+00:00" },
  ];
  for (const body of bodies) {
    const env = makeEnv();
    env.net.route("GET", "/auth/me", json(200, body));
    const view = await env.boot();
    assert.equal(view.screen, "unavailable", JSON.stringify(body));
    assert.equal(env.net.requests("GET", /\/api\//).length, 0, "nothing of the conversations is asked");
  }
  const past = makeEnv(); // a deadline that has passed already: not a session
  past.net.route("GET", "/auth/me", me(-1));
  const view = await past.boot();
  assert.deepEqual([view.screen, view.notice.text], ["login", Logic.TEXT.authExpired]);
  assert.equal(past.net.requests("GET", /\/api\//).length, 0);
});

// --- the record in sessionStorage -----------------------------------------------------------------------------------

scenario("records_that_cannot_be_used_are_removed_from_the_storage_and_hold_no_text", async () => {
  const stored = (storage) => storage.data[Logic.STORAGE_KEY];
  const load = (storage) => Logic.createPendingStore(storage, () => START).load(USER_A);
  for (const value of ["not json", "null", "[]", '{"v":2,"turns":[]}', '{"v":1,"turns":"x"}', '{"v":1}']) {
    const storage = new Storage({ [Logic.STORAGE_KEY]: value });
    assert.deepEqual(load(storage), []);
    assert.equal(stored(storage), undefined, "removed: " + value);
  }

  const mixed = storageWith(
    recordOf({ t: "Годный вопрос" }), recordOf({ t: "Просроченный секрет", at: START - 7 * 3600 * 1000 }),
    recordOf({ t: "Негодный секрет", k: "bad key" }), null
  );
  assert.deepEqual(load(mixed).map((r) => r.text), ["Годный вопрос"]);
  assert.deepEqual(mixed.record.turns.map((t) => t.t), ["Годный вопрос"]);
  assert.ok(!stored(mixed).includes("секрет"), "nothing of the unusable ones is kept");

  const everyone = storageWith(recordOf({ at: START - 7 * 3600 * 1000 }));
  load(everyone);
  assert.equal(stored(everyone), undefined, "when none is usable the key goes");

  const many = storageWith(...Array.from({ length: 12 }, (_, i) => recordOf({ t: "q" + i, k: "web-" + String(i).padStart(32, "0"), at: START - 1000 * (12 - i) })));
  assert.deepEqual(load(many).map((r) => r.text), ["q4", "q5", "q6", "q7", "q8", "q9", "q10", "q11"], "the newest are kept, as when writing");
  assert.equal(many.record.turns.length, 8);

  let writes = 0;
  const clean = storageWith(recordOf());
  const setItem = clean.setItem.bind(clean);
  clean.setItem = (key, value) => (writes++, setItem(key, value));
  load(clean);
  assert.equal(writes, 0, "usable records are left alone");
});

scenario("a_record_that_was_too_old_to_use_is_said_so_and_removed", async () => {
  const storage = storageWith(recordOf({ at: START - 7 * 3600 * 1000, t: "Давнее" }));
  const env = makeEnv({ storage }).signedIn([conversation(CONV_A, 0)]);
  const view = await env.boot();

  assert.deepEqual([view.banner.kind, view.banner.text], ["info", Logic.TEXT.expiredRecords]);
  assert.equal(storage.record, null);
  assert.equal(pendingItem(env), undefined);
});

scenario("when_the_browser_will_not_keep_the_record_the_page_does_not_promise_recovery", async () => {
  for (const options of [{ noStorage: true }, { storage: Object.assign(new Storage(), { broken: true }) }]) {
    const env = makeEnv(options).signedIn([conversation(CONV_A, 0)]);
    await env.boot();
    env.net.route("POST", URL_A, fail());
    await (await env.ctl.send("Вопрос")).done;
    await env.settle();
    assert.equal(pendingItem(env).warning, Logic.TEXT.notSaved);
    assert.deepEqual(pendingItem(env).actions, ["retry"]);
  }
  const kept = await uncertainEnv(fail());
  await (await kept.ctl.send("Вопрос")).done;
  await kept.settle();
  assert.equal(pendingItem(kept).warning, "");
});

// ============================================================================================
// Names of conversations: the first message, as the server names them
// ============================================================================================

const FIRST_QUESTION = "Что приготовить из курицы и риса?";

async function startedConversation(question, answerText = "Запеките.") {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.net.route("POST", "/api/conversations", json(201, conversation(CONV_C, 60)));
  env.net.route("POST", `/api/conversations/${CONV_C}/messages`, turnBody(CONV_C, 1, question, 2, answerText));
  await (await env.ctl.send(question)).done;
  await env.settle();
  return env;
}

scenario("a_conversation_made_in_the_page_is_named_by_its_first_message", async () => {
  const env = await startedConversation(FIRST_QUESTION);

  const view = env.view();
  assert.equal(view.list.items[0].label, FIRST_QUESTION);
  assert.equal(view.chatTitle, FIRST_QUESTION);
  assert.equal(env.ctl._state().list.items[0].title, FIRST_QUESTION);
});

scenario("the_second_message_does_not_rename_the_conversation", async () => {
  const env = await startedConversation(FIRST_QUESTION);
  env.net.route("POST", `/api/conversations/${CONV_C}/messages`, turnBody(CONV_C, 3, "А без духовки?", 4, "На плите."));

  await (await env.ctl.send("А без духовки?")).done;
  await env.settle();

  assert.equal(env.view().list.items[0].label, FIRST_QUESTION);
});

scenario("a_long_or_untidy_first_message_is_named_on_one_line_and_cut_like_the_servers", async () => {
  const long = "Подскажите, пожалуйста, как приготовить домашнюю лапшу для куриного супа без яиц и муки высшего сорта";
  const env = await startedConversation("  " + long + "\n\n  ещё\tстрока ");

  const label = env.view().list.items[0].label;
  assert.equal(label, Logic.deriveTitle(long));
  assert.ok(label.endsWith("…"), label);
  assert.equal(Array.from(label).length, Logic.TITLE_LENGTH + 1);
  assert.ok(!/[\n\t]/.test(label));
});

scenario("a_name_the_server_gave_is_kept_when_the_conversation_is_continued", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0, "Борщ по-домашнему")], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Борщ по-домашнему"), message(2, "assistant", "Варите.")]),
  });
  await env.boot();
  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Сколько солить?", 4, "Две чайные ложки."));

  await (await env.ctl.send("Сколько солить?")).done;
  await env.settle();

  assert.equal(env.view().list.items[0].label, "Борщ по-домашнему");
});

scenario("a_conversation_without_a_name_is_named_when_its_first_message_is_known_and_not_before", async () => {
  // Nothing in it yet: the first exchange names it.
  const empty = makeEnv().signedIn([conversation(CONV_A, 0)], { [CONV_A]: historyBody(CONV_A, []) });
  await empty.boot();
  empty.net.route("POST", URL_A, turnBody(CONV_A, 1, FIRST_QUESTION, 2, "Запеките."));
  await (await empty.ctl.send(FIRST_QUESTION)).done;
  await empty.settle();
  assert.equal(empty.view().list.items[0].label, FIRST_QUESTION);

  // Older messages are still unread: the first message is not known, and a newer one must not name it.
  const longer = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(7, "user", "Вопрос"), message(8, "assistant", "Ответ")], { hasMore: true, nextBefore: 7 }),
  });
  await longer.boot();
  longer.net.route("POST", URL_A, turnBody(CONV_A, 9, "Новое", 10, "Ответ."));
  await (await longer.ctl.send("Новое")).done;
  await longer.settle();
  assert.equal(longer.ctl._state().list.items[0].title, null);
  assert.ok(longer.view().list.items[0].label.startsWith("Разговор от "), "the date, as before");
});

scenario("the_title_rule_is_the_same_for_every_kind_of_whitespace_and_every_length", async () => {
  const cases = [
    ["", null],
    [" \n\t ", null],
    ["Борщ", "Борщ"],
    ["  Борщ   с\t\tфасолью\n", "Борщ с фасолью"],
    ["a b c　d", "a b c d"],
    ["x".repeat(60), "x".repeat(60)],
    ["x".repeat(61), "x".repeat(60) + "…"],
    ["😀".repeat(60), "😀".repeat(60)],
    ["😀".repeat(61), "😀".repeat(60) + "…"],
    ["слово ".repeat(40), ("слово ".repeat(40)).slice(0, 60).replace(/ +$/, "") + "…"],
  ];
  for (const [input, expected] of cases) {
    assert.equal(Logic.deriveTitle(input), expected, JSON.stringify(input.slice(0, 20)));
  }
  assert.equal(Logic.deriveTitle(undefined), null);
  assert.equal(Logic.deriveTitle(5), null);
});

// ============================================================================================
// Favorites
// ============================================================================================

const RECIPE_TEXT = "Борщ\n\nПорции: 4.\n1. Сварите бульон.";
const F1 = H.favorite(H.FAV_1, 2, "Борщ классический", 0);
const F2 = H.favorite(H.FAV_2, 12, "Плов с курицей", 30);
const FAVORITES_URL = "/api/favorites";
const FAV_URL_1 = `/api/favorites/${H.FAV_1}`;
const FAV_URL_2 = `/api/favorites/${H.FAV_2}`;

// A signed-in person with the conversation of withConversation() and these favorites.
async function withFavorites(items = [F1, F2], options = {}) {
  const env = makeEnv(options).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]),
  });
  env.net.route("GET", FAVORITES_URL, H.favoritesBody(items));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(F1, RECIPE_TEXT));
  env.net.route("GET", FAV_URL_2, H.favoriteDetail(F2, "Плов: рис, курица."));
  await env.boot();
  return env;
}

const answerMark = (env) => timeline(env).find((i) => i.role === "assistant").favorite;

scenario("the_favorites_are_read_once_at_the_start_and_mark_the_answers_that_are_kept", async () => {
  const env = await withFavorites([F1]);

  assert.equal(gets(env, FAVORITES_URL), 1);
  const view = env.view();
  assert.equal(view.section, "chats");
  assert.deepEqual([view.favorites.status, view.favorites.items.map((i) => i.title)], ["ready", ["Борщ классический"]]);
  assert.deepEqual(answerMark(env), { state: "saved", id: H.FAV_1, error: null });
  const user = timeline(env).find((i) => i.role === "user");
  assert.equal("favorite" in user, false, "only the assistant's answers can be kept");
});

scenario("an_answer_that_is_not_kept_is_marked_so", async () => {
  const env = await withFavorites([F2]); // (kept: another answer)

  assert.deepEqual(answerMark(env), { state: "none", id: null, error: null });
});

scenario("when_the_favorites_cannot_be_read_the_conversations_work_and_the_list_can_be_read_again", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]),
  });
  env.net.route("GET", FAVORITES_URL, fail(), H.favoritesBody([F1]));
  let view = await env.boot();

  assert.deepEqual([view.favorites.status, view.favorites.error.text], ["error", Logic.TEXT.network]);
  assert.equal(view.composer.locked, false);
  assert.deepEqual(view.thread.items.map((i) => i.text), ["Старый вопрос", "Старый ответ"]);

  await env.ctl.reloadFavorites();
  await env.settle();
  assert.deepEqual([env.view().favorites.status, env.view().favorites.items.length], ["ready", 1]);
});

scenario("a_favorites_list_that_is_not_the_servers_is_an_error_not_an_empty_list", async () => {
  for (const answer of [json(200, { favorites: [{ id: "x" }] }), json(200, { nothing: 1 }), text(502, "<html>"), json(500, { detail: "internal_error" })]) {
    const env = makeEnv().signedIn([]);
    env.net.route("GET", FAVORITES_URL, answer);
    const view = await env.boot();

    assert.equal(view.favorites.status, "error");
    assert.deepEqual(view.favorites.items, []);
  }
});

scenario("keeping_an_answer_sends_the_conversation_and_the_message_and_nothing_else", async () => {
  const env = await withFavorites([]);
  env.net.route("POST", FAVORITES_URL, H.keptBody(F1));

  const result = await env.ctl.keepFavorite(CONV_A, 2);
  await env.settle();

  assert.deepEqual(result, { ok: true, reason: "created" });
  const [request] = env.net.requests("POST", FAVORITES_URL);
  assert.deepEqual(request.body, { conversation_id: CONV_A, message_id: 2 });
  assert.equal(request.init.credentials, "same-origin");
  assert.equal(request.init.cache, "no-store");
  assert.equal(request.headers["idempotency-key"], undefined, "keeping an answer is idempotent by itself");
  assert.deepEqual(answerMark(env), { state: "saved", id: H.FAV_1, error: null });
  assert.deepEqual(env.view().favorites.items.map((i) => i.id), [H.FAV_1]);
  assert.ok(env.announced.includes(Logic.TEXT.favoriteKeptAnnounce));
});

scenario("while_an_answer_is_being_kept_it_says_so_and_asking_again_sends_nothing", async () => {
  const env = await withFavorites([]);
  const gate = deferred();
  env.net.route("POST", FAVORITES_URL, async () => {
    await gate.promise;
    return H.keptBody(F1);
  });

  const first = env.ctl.keepFavorite(CONV_A, 2);
  await flush();
  assert.deepEqual(answerMark(env), { state: "saving", id: null, error: null });
  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2), { ok: false, reason: "busy" });
  gate.resolve();
  await first;
  await env.settle();

  assert.equal(env.net.requests("POST", FAVORITES_URL).length, 1);
  assert.equal(answerMark(env).state, "saved");
});

scenario("an_answer_that_is_already_kept_is_not_sent_again", async () => {
  const env = await withFavorites([F1]);

  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2), { ok: true, reason: "already" });
  assert.equal(env.net.requests("POST", FAVORITES_URL).length, 0);
});

scenario("a_keep_whose_outcome_is_unclear_can_simply_be_asked_again", async () => {
  const env = await withFavorites([]);
  env.net.route("POST", FAVORITES_URL, fail(), apiError(503, "service_unavailable", { retryable: true }), H.keptBody(F1, false));

  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2), { ok: false, reason: "uncertain" });
  assert.deepEqual(answerMark(env), { state: "error", id: null, error: { text: Logic.TEXT.network, correlationId: null } });
  assert.equal((await env.ctl.keepFavorite(CONV_A, 2)).ok, false);
  assert.equal(answerMark(env).state, "error");
  assert.equal(answerMark(env).error.correlationId !== null, true, "the number of the request is kept for a report");

  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2), { ok: true, reason: "already" }); // 200: it was kept after all
  assert.equal(answerMark(env).state, "saved");
  assert.equal(env.net.requests("POST", FAVORITES_URL).length, 3);
});

scenario("a_refusal_is_shown_in_the_servers_words_and_a_stranger_answer_is_not_a_success", async () => {
  const cases = [
    [apiError(409, "favorites_full"), Logic.CODE_MESSAGES.favorites_full],
    [apiError(404, "message_not_found"), Logic.CODE_MESSAGES.message_not_found],
    [apiError(404, "conversation_not_found"), Logic.CODE_MESSAGES.conversation_not_found],
    [json(201, { id: "no-uuid" }), Logic.TEXT.unexpected],
    [text(200, "<html>proxy</html>"), Logic.TEXT.unexpected],
  ];
  for (const [answer, words] of cases) {
    const env = await withFavorites([]);
    env.net.route("POST", FAVORITES_URL, answer);

    const result = await env.ctl.keepFavorite(CONV_A, 2);

    assert.equal(result.ok, false);
    assert.equal(answerMark(env).state, "error");
    assert.equal(answerMark(env).error.text, words);
    assert.deepEqual(env.view().favorites.items, [], "nothing is shown as kept that is not");
  }
});

scenario("keeping_ends_the_session_when_the_server_says_401", async () => {
  const env = await withFavorites([]);
  env.net.route("POST", FAVORITES_URL, notAuthenticated());

  await env.ctl.keepFavorite(CONV_A, 2);
  await env.settle();

  const view = env.view();
  assert.equal(view.screen, "login");
  assert.deepEqual(view.favorites.items, []);
  assert.equal(view.currentId, null);
});

scenario("only_a_conversation_and_a_message_that_can_be_named_are_sent", async () => {
  const env = await withFavorites([]);

  for (const [conversationId, messageId] of [["x", 2], [CONV_A, 0], [CONV_A, -1], [CONV_A, 1.5], [CONV_A, "2"], [null, 2], [CONV_A, undefined]]) {
    assert.deepEqual(await env.ctl.keepFavorite(conversationId, messageId), { ok: false, reason: "invalid" });
  }
  assert.equal(env.net.requests("POST", FAVORITES_URL).length, 0);
});

scenario("the_favorites_section_lists_newest_first_and_a_recipe_is_opened_by_asking_the_server", async () => {
  const env = await withFavorites([F2, F1]);

  env.ctl.showSection("favorites");
  await env.settle();
  let view = env.view();
  assert.equal(view.section, "favorites");
  assert.deepEqual(view.favorites.items.map((i) => i.title), ["Плов с курицей", "Борщ классический"]);
  assert.ok(view.favorites.items[0].meta.startsWith("Сохранено: "), view.favorites.items[0].meta);
  assert.equal(view.favorites.current, null);

  await env.ctl.openFavorite(H.FAV_1);
  await env.settle();

  view = env.view();
  assert.equal(view.section, "favorites");
  assert.deepEqual(
    [view.favorites.current.id, view.favorites.current.status, view.favorites.current.title, view.favorites.current.text],
    [H.FAV_1, "ready", "Борщ классический", RECIPE_TEXT]
  );
  assert.deepEqual(view.favorites.items.map((i) => i.current), [false, true]);
  assert.equal(gets(env, FAV_URL_1), 1);
  assert.equal(gets(env, FAVORITES_URL), 2, "the list is read at the start and again when Favorites is opened");
});

scenario("a_recipe_that_is_loading_is_shown_with_its_title_from_the_list", async () => {
  const env = await withFavorites([F1]);
  const gate = deferred();
  env.net.route("GET", FAV_URL_1, async () => {
    await gate.promise;
    return H.favoriteDetail(F1, RECIPE_TEXT);
  });

  const opening = env.ctl.openFavorite(H.FAV_1);
  await flush();
  assert.deepEqual(
    [env.view().favorites.current.status, env.view().favorites.current.title, env.view().favorites.current.text],
    ["loading", "Борщ классический", ""]
  );
  gate.resolve();
  await opening;
  assert.equal(env.view().favorites.current.status, "ready");
});

scenario("a_recipe_that_cannot_be_opened_offers_to_try_again", async () => {
  const env = await withFavorites([F1]);
  env.net.route("GET", FAV_URL_1, fail(), H.favoriteDetail(F1, RECIPE_TEXT));

  await env.ctl.openFavorite(H.FAV_1);
  assert.deepEqual([env.view().favorites.current.status, env.view().favorites.current.error.text], ["error", Logic.TEXT.network]);

  await env.ctl.reloadRecipe();
  assert.deepEqual([env.view().favorites.current.status, env.view().favorites.current.text], ["ready", RECIPE_TEXT]);
});

scenario("a_recipe_removed_elsewhere_is_said_to_be_gone_and_leaves_the_list", async () => {
  const env = await withFavorites([F1, F2]);
  env.net.route("GET", FAV_URL_1, apiError(404, "favorite_not_found"));

  await env.ctl.openFavorite(H.FAV_1);

  const view = env.view();
  assert.deepEqual([view.favorites.current.status, view.favorites.current.error.text], ["gone", Logic.TEXT.recipeGone]);
  assert.deepEqual(view.favorites.items.map((i) => i.id), [H.FAV_2]);
});

scenario("a_late_answer_about_a_recipe_that_is_no_longer_the_open_one_changes_nothing", async () => {
  const env = await withFavorites([F1, F2]);
  const slow = deferred();
  env.net.route("GET", FAV_URL_1, async () => {
    await slow.promise;
    return H.favoriteDetail(F1, RECIPE_TEXT);
  });

  const first = env.ctl.openFavorite(H.FAV_1);
  await flush();
  await env.ctl.openFavorite(H.FAV_2);
  slow.resolve();
  await first;
  await env.settle();

  const current = env.view().favorites.current;
  assert.deepEqual([current.id, current.status, current.text], [H.FAV_2, "ready", "Плов: рис, курица."]);
});

scenario("an_answer_for_another_recipe_is_not_taken_for_the_one_asked_for", async () => {
  const env = await withFavorites([F1, F2]);
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(F2, "Чужой текст")); // the server named another one

  await env.ctl.openFavorite(H.FAV_1);

  assert.equal(env.view().favorites.current.status, "error");
  assert.equal(env.view().favorites.current.text, "");
});

scenario("only_a_favorite_id_that_can_be_one_is_asked_for", async () => {
  const env = await withFavorites([F1]);

  for (const id of ["x", "../../auth/me", "", null, undefined, 5, `${H.FAV_1}/../x`]) {
    await env.ctl.openFavorite(id);
  }

  assert.equal(env.net.log.filter((r) => /^\/api\/favorites\/.+/.test(r.url)).length, 0);
  assert.equal(env.view().section, "chats");
});

scenario("opening_a_conversation_brings_the_conversations_forward_again", async () => {
  const env = await withFavorites([F1]);
  await env.ctl.openFavorite(H.FAV_1);
  assert.equal(env.view().section, "favorites");

  await env.ctl.selectConversation(CONV_A);
  assert.equal(env.view().section, "chats");
  await env.ctl.openFavorite(H.FAV_1);
  await env.ctl.startDraft();
  assert.equal(env.view().section, "chats");
  env.ctl.showSection("favorites");
  assert.equal(env.view().section, "favorites");
  env.ctl.showSection("nonsense");
  assert.equal(env.view().section, "favorites");
});

scenario("the_favorites_section_reads_the_list_when_the_first_read_failed", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", FAVORITES_URL, fail(), H.favoritesBody([F1]));
  await env.boot();
  assert.equal(env.view().favorites.status, "error");

  env.ctl.showSection("favorites");
  await env.settle();

  assert.deepEqual([env.view().favorites.status, env.view().favorites.items.length], ["ready", 1]);
  assert.equal(gets(env, FAVORITES_URL), 2);
});

scenario("removing_asks_first_and_changes_nothing_until_it_is_confirmed", async () => {
  const env = await withFavorites([F1, F2]);
  await env.ctl.openFavorite(H.FAV_1);

  env.ctl.askRemoveFavorite(H.FAV_1);
  assert.deepEqual(env.view().favorites.current.removing, { phase: "asking", error: null });
  env.ctl.cancelRemoveFavorite();
  assert.equal(env.view().favorites.current.removing, null);
  env.ctl.askRemoveFavorite(H.FAV_2); // not the recipe that is open
  assert.equal(env.view().favorites.current.removing, null);
  assert.deepEqual(await env.ctl.removeFavorite(), { ok: false, reason: "not_asked" });

  assert.equal(env.net.requests("DELETE", /.*/).length, 0);
  assert.deepEqual(env.view().favorites.items.map((i) => i.id), [H.FAV_1, H.FAV_2]);
});

scenario("a_confirmed_removal_deletes_that_recipe_only", async () => {
  const env = await withFavorites([F1, F2]);
  env.net.route("DELETE", FAV_URL_1, H.removedBody());
  await env.ctl.openFavorite(H.FAV_1);

  env.ctl.askRemoveFavorite(H.FAV_1);
  const result = await env.ctl.removeFavorite();
  await env.settle();

  assert.deepEqual(result, { ok: true });
  const [request] = env.net.requests("DELETE", /.*/);
  assert.equal(request.url, FAV_URL_1);
  assert.equal(request.body, undefined, "nothing but the address says what to remove");
  assert.equal(request.init.credentials, "same-origin");
  const view = env.view();
  assert.deepEqual(view.favorites.items.map((i) => i.id), [H.FAV_2]);
  assert.equal(view.favorites.current, null);
  assert.deepEqual(answerMark(env), { state: "none", id: null, error: null }, "the answer can be kept again");
  assert.ok(env.announced.includes(Logic.TEXT.removed));
});

scenario("a_removal_that_failed_keeps_the_recipe_and_may_be_repeated", async () => {
  const env = await withFavorites([F1]);
  env.net.route("DELETE", FAV_URL_1, apiError(503, "service_unavailable", { retryable: true }), H.removedBody());
  await env.ctl.openFavorite(H.FAV_1);
  env.ctl.askRemoveFavorite(H.FAV_1);

  assert.deepEqual(await env.ctl.removeFavorite(), { ok: false, reason: "failed" });
  let current = env.view().favorites.current;
  assert.equal(current.removing.phase, "error");
  assert.equal(current.removing.error.text, Logic.CODE_MESSAGES.service_unavailable);
  assert.equal(current.status, "ready");
  assert.deepEqual(env.view().favorites.items.map((i) => i.id), [H.FAV_1]);

  assert.deepEqual(await env.ctl.removeFavorite(), { ok: true });
  assert.equal(env.view().favorites.current, null);
  assert.deepEqual(env.view().favorites.items, []);
});

scenario("removing_what_is_already_gone_is_the_outcome_that_was_wanted", async () => {
  const env = await withFavorites([F1]);
  env.net.route("DELETE", FAV_URL_1, apiError(404, "favorite_not_found"));
  await env.ctl.openFavorite(H.FAV_1);
  env.ctl.askRemoveFavorite(H.FAV_1);

  assert.deepEqual(await env.ctl.removeFavorite(), { ok: true });
  assert.deepEqual(env.view().favorites.items, []);
});

scenario("a_removal_is_not_sent_twice_while_it_is_under_way", async () => {
  const env = await withFavorites([F1]);
  const gate = deferred();
  env.net.route("DELETE", FAV_URL_1, async () => {
    await gate.promise;
    return H.removedBody();
  });
  await env.ctl.openFavorite(H.FAV_1);
  env.ctl.askRemoveFavorite(H.FAV_1);

  const first = env.ctl.removeFavorite();
  await flush();
  assert.equal(env.view().favorites.current.removing.phase, "removing");
  assert.deepEqual(await env.ctl.removeFavorite(), { ok: false, reason: "not_asked" });
  env.ctl.cancelRemoveFavorite();
  assert.equal(env.view().favorites.current.removing.phase, "removing", "it cannot be called back");
  gate.resolve();
  await first;

  assert.equal(env.net.requests("DELETE", /.*/).length, 1);
});

scenario("opening_another_recipe_drops_the_question_about_the_first", async () => {
  const env = await withFavorites([F1, F2]);
  await env.ctl.openFavorite(H.FAV_1);
  env.ctl.askRemoveFavorite(H.FAV_1);

  await env.ctl.openFavorite(H.FAV_2);

  assert.equal(env.view().favorites.current.removing, null);
  assert.deepEqual(await env.ctl.removeFavorite(), { ok: false, reason: "not_asked" });
  assert.equal(env.net.requests("DELETE", /.*/).length, 0);
});

scenario("a_401_on_any_favorites_call_ends_the_session_and_drops_every_favorite", async () => {
  for (const [route, act] of [
    [["GET", FAV_URL_1], (env) => env.ctl.openFavorite(H.FAV_1)],
    [["DELETE", FAV_URL_1], async (env) => {
      await env.ctl.openFavorite(H.FAV_1);
      env.ctl.askRemoveFavorite(H.FAV_1);
      await env.ctl.removeFavorite();
    }],
  ]) {
    const env = await withFavorites([F1]);
    env.net.route(route[0], route[1], notAuthenticated());

    await act(env);
    await env.settle();

    const view = env.view();
    assert.equal(view.screen, "login");
    assert.deepEqual(view.favorites.items, []);
    assert.equal(view.favorites.current, null);
  }
});

scenario("leaving_drops_the_favorites_with_everything_else_and_a_new_person_starts_with_none_of_them", async () => {
  const env = await withFavorites([F1]);
  await env.ctl.openFavorite(H.FAV_1);
  env.net.route("POST", "/auth/logout", { kind: "empty", status: 204 });

  await env.ctl.logout();

  assert.equal(env.view().screen, "login");
  const state = env.ctl._state();
  assert.deepEqual([state.section, state.favorites.items, state.favorites.currentId, state.favorites.detail], ["chats", [], null, null]);
  assert.ok(!JSON.stringify(state).includes("Борщ"), "no word of a recipe is left in memory");
});

scenario("nothing_of_the_favorites_is_in_view_while_the_page_asks_who_is_signed_in", async () => {
  const env = await withFavorites([F1]);
  await env.ctl.openFavorite(H.FAV_1);
  const gate = deferred();
  env.net.route("GET", "/auth/me", async () => {
    await gate.promise;
    return me();
  });

  env.ctl.recheck();
  const held = env.view();
  assert.equal(held.screen, "verifying");
  assert.deepEqual([held.section, held.favorites.items, held.favorites.current], ["chats", [], null]);
  assert.ok(!JSON.stringify(held).includes("Борщ"));
  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2), { ok: false, reason: "not_ready" });
  assert.equal(await env.ctl.openFavorite(H.FAV_2), undefined);
  gate.resolve();
  await env.settle();

  const back = env.view();
  assert.equal(back.screen, "app");
  assert.deepEqual([back.section, back.favorites.current.id, back.favorites.items.length], ["favorites", H.FAV_1, 1]);
});

scenario("a_different_person_never_sees_the_last_persons_favorites", async () => {
  const env = await withFavorites([F1]);
  await env.ctl.openFavorite(H.FAV_1);
  env.net.route("GET", "/auth/me", me(86400, USER_B));
  env.net.route("GET", FAVORITES_URL, H.favoritesBody([F2]));

  env.ctl.recheck();
  await env.settle();

  const view = env.view();
  assert.equal(view.screen, "app");
  assert.equal(view.section, "chats");
  assert.deepEqual(view.favorites.items.map((i) => i.title), ["Плов с курицей"]);
  assert.equal(view.favorites.current, null);
  assert.ok(!JSON.stringify(view).includes("Борщ"));
});

scenario("favorites_are_never_written_to_the_browsers_storage", async () => {
  const env = await withFavorites([F1]);
  env.net.route("POST", FAVORITES_URL, H.keptBody(F2));
  await env.ctl.openFavorite(H.FAV_1);
  await env.ctl.keepFavorite(CONV_A, 12);
  await env.settle();

  assert.deepEqual(env.storage.data, {});
});

scenario("every_favorites_call_is_made_with_the_session_cookie_only_and_names_no_user", async () => {
  const env = await withFavorites([F1]);
  env.net.route("POST", FAVORITES_URL, H.keptBody(F2));
  env.net.route("DELETE", FAV_URL_1, H.removedBody());
  await env.ctl.openFavorite(H.FAV_1);
  await env.ctl.keepFavorite(CONV_A, 12);
  env.ctl.askRemoveFavorite(H.FAV_1);
  await env.ctl.removeFavorite();

  const asked = env.net.log.filter((r) => r.url.startsWith(FAVORITES_URL));
  assert.deepEqual(asked.map((r) => r.method), ["GET", "GET", "POST", "DELETE"]);
  for (const request of asked) {
    assert.equal(request.init.credentials, "same-origin");
    assert.equal(request.init.redirect, "error");
    assert.deepEqual(Object.keys(request.headers).sort(), request.body === undefined ? ["accept"] : ["accept", "content-type"]);
    assert.ok(!JSON.stringify(request).includes(USER_A));
  }
});

// --- structured recipes: one Save per recipe, and the editor ---------------------------------

const BORSH_RECIPE = {
  title: "Борщ",
  servings: "4 порции",
  ingredients: [
    { name: "свекла", amount: "2 шт." },
    { name: "капуста", amount: "300 г" },
  ],
  steps: ["Сварите бульон.", "Добавьте овощи."],
};
const UKHA_RECIPE = {
  title: "Уха",
  servings: null,
  ingredients: [{ name: "рыба", amount: "1 кг" }],
  steps: ["Сварите.", "Дайте настояться."],
};
const STRUCTURED_TEXT = "Борщ\n\nИнгредиенты:\n- свекла — 2 шт.\n\nПриготовление:\n1. Сварите бульон.";

function structuredAnswer(recipes = [BORSH_RECIPE, UKHA_RECIPE]) {
  return message(2, "assistant", STRUCTURED_TEXT, {
    recipes: recipes.map((recipe, position) => ({ position, title: recipe.title })),
  });
}

async function withStructuredAnswer(options = {}) {
  const env = makeEnv({ structuredRecipes: true, ...options }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что приготовить?"), structuredAnswer()]),
  });
  await env.boot();
  return env;
}

function structuredFavoriteItem(extra = {}) {
  return H.structuredFavorite(H.FAV_1, 2, "Борщ", 0, 0, {
    servings: "4 порции",
    ingredients: BORSH_RECIPE.ingredients,
    steps: BORSH_RECIPE.steps,
    notes: "Больше укропа.",
    version: 3,
    ...extra,
  });
}

async function withOpenStructuredFavorite(extra = {}) {
  const item = structuredFavoriteItem(extra);
  const env = makeEnv({ structuredRecipes: true }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что?"), structuredAnswer()]),
  });
  env.net.route("GET", FAVORITES_URL, H.favoritesBody([item]));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(item, STRUCTURED_TEXT));
  await env.boot();
  await env.ctl.openFavorite(H.FAV_1);
  await env.settle();
  return { env, item };
}

const assistantItem = (env) => timeline(env).find((item) => item.role === "assistant");

scenario("structured_each_recipe_of_one_answer_has_its_own_save_control", async () => {
  const env = await withStructuredAnswer();
  let answer = assistantItem(env);

  assert.equal(answer.favorite, null, "no whole-answer button when the answer has recipes");
  assert.deepEqual(
    answer.recipes.map((recipe) => [recipe.position, recipe.title, recipe.state]),
    [[0, "Борщ", "none"], [1, "Уха", "none"]]
  );

  env.net.route("POST", FAVORITES_URL, H.keptBody(structuredFavoriteItem()));
  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2, 0), { ok: true, reason: "created" });
  assert.deepEqual(env.net.requests("POST", FAVORITES_URL)[0].body, {
    conversation_id: CONV_A,
    message_id: 2,
    recipe_index: 0,
  });

  answer = assistantItem(env);
  assert.deepEqual(answer.recipes.map((recipe) => recipe.state), ["saved", "none"]);
  assert.equal(answer.recipes[1].favoriteId, null);

  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2, 0), { ok: true, reason: "already" });
  assert.equal(env.net.requests("POST", FAVORITES_URL).length, 1, "a kept recipe is never saved twice");
});

scenario("structured_a_fresh_answer_shows_its_recipe_controls_without_a_reload", async () => {
  const env = await withConversation({ structuredRecipes: true });
  env.net.route("POST", URL_A, turnBody(CONV_A, 3, "Еще?", 4, "Вот.", false, [{ position: 0, title: "Уха" }]));

  assert.equal((await env.ctl.send("Еще?")).ok, true);
  await env.settle();

  const answer = timeline(env).find((item) => item.type === "message" && item.id === 4);
  assert.deepEqual(answer.recipes.map((recipe) => [recipe.position, recipe.title, recipe.state]), [[0, "Уха", "none"]]);
  assert.equal(answer.favorite, null);
});

scenario("structured_two_save_controls_are_independent_in_failure_and_success", async () => {
  const env = await withStructuredAnswer();
  env.net.route(
    "POST",
    FAVORITES_URL,
    apiError(409, "favorites_full"),
    H.keptBody(H.structuredFavorite(H.FAV_2, 2, "Уха", 1))
  );

  assert.equal((await env.ctl.keepFavorite(CONV_A, 2, 0)).reason, "rejected");
  let answer = assistantItem(env);
  assert.deepEqual(answer.recipes.map((recipe) => recipe.state), ["error", "none"]);
  assert.equal(answer.recipes[0].error.text, Logic.CODE_MESSAGES.favorites_full);

  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2, 1), { ok: true, reason: "created" });
  answer = assistantItem(env);
  assert.deepEqual(answer.recipes.map((recipe) => recipe.state), ["error", "saved"]);
});

scenario("structured_off_an_ordinary_answer_keeps_the_legacy_whole_answer_save", async () => {
  const env = await withConversation(); // (the switch is off, the default)
  const answer = assistantItem(env);

  assert.deepEqual(answer.recipes, [], "no per-recipe controls while generation is off");
  assert.deepEqual(answer.favorite, { state: "none", id: null, error: null }, "the legacy save stays");

  env.net.route("POST", FAVORITES_URL, H.keptBody(F1));
  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2), { ok: true, reason: "created" });
  const sent = env.net.requests("POST", FAVORITES_URL)[0].body;
  assert.deepEqual(sent, { conversation_id: CONV_A, message_id: 2 }, "no recipe identity is fabricated");
});

scenario("structured_an_ordinary_answer_has_no_save_control_at_all", async () => {
  const env = await withConversation({ structuredRecipes: true });
  const answer = assistantItem(env);

  assert.deepEqual(answer.recipes, []);
  assert.equal(answer.favorite, null, "no whole-answer Save, and no legacy fallback");
  assert.equal(env.net.requests("POST", FAVORITES_URL).length, 0, "nothing is offered to send");
});

scenario("structured_one_recipe_in_the_history_has_one_independent_save_control", async () => {
  const env = makeEnv({ structuredRecipes: true }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [
      message(1, "user", "Что приготовить?"),
      structuredAnswer([UKHA_RECIPE]),
    ]),
  });
  await env.boot();

  let answer = assistantItem(env);
  assert.equal(answer.favorite, null);
  assert.deepEqual(
    answer.recipes.map((recipe) => [recipe.position, recipe.title, recipe.state]),
    [[0, "Уха", "none"]]
  );

  env.net.route("POST", FAVORITES_URL, H.keptBody(H.structuredFavorite(H.FAV_2, 2, "Уха", 0)));
  assert.deepEqual(await env.ctl.keepFavorite(CONV_A, 2, 0), { ok: true, reason: "created" });

  answer = assistantItem(env);
  assert.deepEqual(answer.recipes.map((recipe) => [recipe.position, recipe.state]), [[0, "saved"]]);
  assert.equal(answer.favorite, null, "one recipe never brings the whole-answer Save back");
});

scenario("structured_a_malformed_recipe_list_offers_no_save_control", async () => {
  // The server stores only validated recipes, but a page must not turn an answer whose
  // recipe list is unrecognized into a whole-answer Save: it offers nothing.
  const malformed = [
    "Борщ",
    null,
    { position: 0, title: "Борщ" },
    [{ position: "0", title: "Борщ" }, { position: -1, title: "Борщ" }, { position: 0, title: "" }, { position: 0 }],
    [{ title: "Борщ" }, { position: 9, title: "Борщ" }, "Борщ", null],
  ];
  for (const recipes of malformed) {
    const env = makeEnv({ structuredRecipes: true }).signedIn([conversation(CONV_A, 0)], {
      [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что?"), message(2, "assistant", "Ответ.", { recipes })]),
    });
    await env.boot();
    const answer = assistantItem(env);
    const label = JSON.stringify(recipes);

    assert.deepEqual(answer.recipes, [], label);
    assert.equal(answer.favorite, null, "no fallback to the whole-answer save: " + label);
  }
});

scenario("structured_a_replayed_answer_shows_the_same_controls_and_no_whole_answer_save", async () => {
  const env = await withConversation({ structuredRecipes: true });
  const recipes = [{ position: 0, title: "Уха" }];
  env.net.route("POST", URL_A, fail(), turnBody(CONV_A, 3, "Еще?", 4, "Вот.", true, recipes));

  const sent = await env.ctl.send("Еще?");
  await sent.done;
  await env.settle();
  await env.ctl.retry(CONV_A);
  await env.settle();

  const answer = timeline(env).find((item) => item.type === "message" && item.id === 4);
  assert.deepEqual(
    answer.recipes.map((recipe) => [recipe.position, recipe.title, recipe.state]),
    [[0, "Уха", "none"]]
  );
  assert.equal(answer.favorite, null);
  assert.equal(new Set(env.keys(CONV_A)).size, 1, "the same key: a replay, not a second run");
});

scenario("structured_off_a_structured_favorite_still_opens_and_edits", async () => {
  // The switch only changes what the chat offers; the favorites routes and the card are
  // not gated by it.
  const item = structuredFavoriteItem();
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что?"), message(2, "assistant", STRUCTURED_TEXT)]),
  });
  env.net.route("GET", FAVORITES_URL, H.favoritesBody([item]));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(item, STRUCTURED_TEXT));
  await env.boot();
  await env.ctl.openFavorite(H.FAV_1);
  await env.settle();

  let current = env.view().favorites.current;
  assert.deepEqual(
    [current.kind, current.title, current.servings],
    ["structured", "Борщ", "4 порции"],
    "a stored structured favorite is still readable with the switch off"
  );

  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { notes: "Поправка" });
  const updated = structuredFavoriteItem({ notes: "Поправка", version: 4 });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: true });

  assert.deepEqual(env.net.requests("PATCH", FAV_URL_1)[0].body, {
    expected_version: 3,
    title: "Борщ",
    notes: "Поправка",
    servings: "4 порции",
    ingredients: BORSH_RECIPE.ingredients,
    steps: BORSH_RECIPE.steps,
  });
  current = env.view().favorites.current;
  assert.equal(current.editor, null);
  assert.equal(current.notes, "Поправка");
  assert.deepEqual(
    assistantItem(env).favorite,
    { state: "none", id: null, error: null },
    "the chat's legacy control is unchanged with the switch off"
  );
});

scenario("structured_the_controls_disappear_when_the_feature_is_off", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что?"), structuredAnswer()]),
  });
  await env.boot();
  const answer = assistantItem(env);

  assert.deepEqual(answer.recipes, [], "stored recipes are not controls while generation is off");
  assert.deepEqual(answer.favorite, { state: "none", id: null, error: null });
});

scenario("structured_a_saved_recipe_opens_from_the_chat_and_the_card_has_its_fields", async () => {
  const { env } = await withOpenStructuredFavorite();
  const current = env.view().favorites.current;

  assert.equal(env.view().section, "favorites");
  assert.deepEqual(
    [current.kind, current.title, current.servings, current.version],
    ["structured", "Борщ", "4 порции", 3]
  );
  assert.deepEqual(current.ingredients, BORSH_RECIPE.ingredients);
  assert.deepEqual(current.steps, BORSH_RECIPE.steps);
  assert.equal(current.notes, "Больше укропа.");
  assert.equal(current.editor, null);
});

scenario("structured_the_editor_saves_every_edited_field_and_no_model_is_named", async () => {
  const { env } = await withOpenStructuredFavorite();
  assert.equal(env.ctl.hasUnsavedChanges(), false);
  assert.equal(env.ctl.beginEditFavorite(), true);
  assert.equal(env.ctl.hasUnsavedChanges(), true);

  let current = env.view().favorites.current;
  assert.equal(current.editor.phase, "editing");
  assert.deepEqual(current.editor.draft.ingredients, BORSH_RECIPE.ingredients);
  assert.deepEqual(current.editor.draft.steps, BORSH_RECIPE.steps);

  const draft = {
    title: "Борщ по-домашнему",
    notes: "Еще укропа.",
    servings: "6 порций",
    ingredients: [
      { name: "свекла", amount: "3 шт." },
      { name: "капуста", amount: "400 г" },
    ],
    steps: ["Сварите бульон.", "Добавьте овощи.", "Дайте настояться."],
  };
  const updated = structuredFavoriteItem({
    title: draft.title,
    servings: draft.servings,
    ingredients: draft.ingredients,
    steps: draft.steps,
    notes: draft.notes,
    version: 4,
  });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, "новый текст"));

  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: true });
  assert.deepEqual(env.net.requests("PATCH", FAV_URL_1)[0].body, {
    expected_version: 3,
    title: "Борщ по-домашнему",
    notes: "Еще укропа.",
    servings: "6 порций",
    ingredients: draft.ingredients,
    steps: draft.steps,
  });
  current = env.view().favorites.current;
  assert.equal(current.editor, null);
  assert.equal(env.ctl.hasUnsavedChanges(), false);
  assert.deepEqual([current.title, current.version, current.notes], ["Борщ по-домашнему", 4, "Еще укропа."]);
  assert.ok(env.announced.includes(Logic.TEXT.editSaved));
});

scenario("structured_a_conflict_keeps_the_draft_and_offers_reload_or_overwrite", async () => {
  const { env, item } = await withOpenStructuredFavorite();
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, {
    title: "Черновик, который нельзя потерять",
  });
  env.net.route("PATCH", FAV_URL_1, apiError(409, "favorite_conflict"));

  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: false, reason: "conflict" });
  let current = env.view().favorites.current;
  assert.equal(current.editor.phase, "conflict");
  assert.equal(current.editor.draft.title, "Черновик, который нельзя потерять");
  assert.equal(current.editor.error.text, Logic.TEXT.editConflict);

  const changed = structuredFavoriteItem({ title: "Борщ, измененный в другом месте", version: 4 });
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(changed, STRUCTURED_TEXT));
  await env.ctl.reloadFavoriteAfterConflict();
  await env.settle();

  current = env.view().favorites.current;
  assert.equal(current.editor, null);
  assert.deepEqual([current.title, current.version, item.title], ["Борщ, измененный в другом месте", 4, "Борщ"]);
});

scenario("structured_overwrite_reads_the_current_version_first_and_names_it", async () => {
  const { env } = await withOpenStructuredFavorite();
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { title: "Поверх" });
  env.net.route("PATCH", FAV_URL_1, apiError(409, "favorite_conflict"));
  await env.ctl.saveFavoriteEdit(draft);
  assert.equal(env.view().favorites.current.editor.phase, "conflict");

  const latest = structuredFavoriteItem({ title: "Изменено", version: 7 });
  const saved = structuredFavoriteItem({ title: "Поверх", version: 8 });
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(latest, STRUCTURED_TEXT));
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(saved, STRUCTURED_TEXT));
  // The person may edit the conflict view itself: the fields as they are now are what
  // "Сохранить поверх" sends.
  assert.deepEqual(
    await env.ctl.overwriteFavorite(Object.assign({}, draft, { notes: "Правка в окне конфликта" })),
    { ok: true }
  );

  const patches = env.net.requests("PATCH", FAV_URL_1);
  assert.equal(patches.length, 2);
  assert.equal(patches[1].body.expected_version, 7, "the overwrite names the version it just read");
  assert.equal(patches[1].body.title, "Поверх");
  assert.equal(patches[1].body.notes, "Правка в окне конфликта");
  assert.equal(env.view().favorites.current.editor, null);
  assert.equal(env.view().favorites.current.title, "Поверх");
});

scenario("structured_a_network_error_keeps_the_draft_and_a_second_save_works", async () => {
  const { env } = await withOpenStructuredFavorite();
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { notes: "Не потерять" });
  env.net.route("PATCH", FAV_URL_1, fail());
  assert.equal((await env.ctl.saveFavoriteEdit(draft)).reason, "uncertain");

  let current = env.view().favorites.current;
  assert.equal(current.editor.phase, "editing");
  assert.equal(current.editor.draft.notes, "Не потерять");
  assert.equal(current.editor.error.text, Logic.TEXT.network);

  env.net.route(
    "PATCH",
    FAV_URL_1,
    H.favoriteDetail(structuredFavoriteItem({ notes: "Не потерять", version: 4 }), STRUCTURED_TEXT)
  );
  assert.deepEqual(await env.ctl.saveFavoriteEdit(current.editor.draft), { ok: true });
  assert.equal(env.view().favorites.current.editor, null);
});

scenario("structured_the_legacy_editor_sends_title_text_and_notes_and_never_structured_fields", async () => {
  const item = H.favorite(H.FAV_1, 2, "Борщ классический", 0, { version: 2 });
  const env = makeEnv({ structuredRecipes: true }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что?"), message(2, "assistant", RECIPE_TEXT)]),
  });
  env.net.route("GET", FAVORITES_URL, H.favoritesBody([item]));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(item, RECIPE_TEXT));
  await env.boot();
  assert.equal(assistantItem(env).favorite, null, "the whole-answer row is not the chat's fallback under the switch");
  assert.deepEqual(env.view().favorites.items.map((favorite) => favorite.id), [H.FAV_1]);
  await env.ctl.openFavorite(H.FAV_1);
  await env.settle();

  assert.equal(env.view().favorites.current.kind, "text");
  env.ctl.beginEditFavorite();
  const draft = {
    title: "Мой борщ",
    text: "Борщ по-моему.\n\n1. Сварить.",
    notes: "Больше свеклы.",
    servings: "не надо",
    ingredients: [{ name: "не", amount: "надо" }],
    steps: ["не надо"],
  };
  env.net.route(
    "PATCH",
    FAV_URL_1,
    H.favoriteDetail(
      H.favorite(H.FAV_1, 2, "Мой борщ", 0, { version: 3 }),
      draft.text,
      { notes: draft.notes }
    )
  );
  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: true });

  assert.deepEqual(env.net.requests("PATCH", FAV_URL_1)[0].body, {
    expected_version: 2,
    title: "Мой борщ",
    text: draft.text,
    notes: "Больше свеклы.",
  });
  assert.equal(env.view().favorites.current.text, draft.text);
});

scenario("structured_a_draft_survives_a_session_recheck_and_typing_redraws_nothing", async () => {
  const { env } = await withOpenStructuredFavorite();
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, {
    title: "UNSAVED REVIEW DRAFT",
    notes: "Черновик заметки",
    ingredients: [
      { name: "свекла", amount: "2 шт." },
      { name: "морковь", amount: "2 шт." },
    ],
    steps: ["Первый.", "Второй.", "Третий."],
  });
  const changes = env.changes.count;

  // The page hands over every keystroke: the draft is kept, and nothing is redrawn or sent.
  assert.equal(env.ctl.syncFavoriteDraft(draft), true);
  assert.equal(env.changes.count, changes, "typing redraws nothing");
  assert.equal(env.net.requests("PATCH", FAV_URL_1).length, 0);

  // The tab comes back: the session is asked about again, the page is held back while it
  // is, and the same person gets the editor back as it was left.
  env.ctl.recheck();
  assert.equal(env.view().screen, "verifying");
  await env.settle();

  const current = env.view().favorites.current;
  assert.equal(env.view().screen, "app");
  assert.equal(current.editor.phase, "editing");
  assert.equal(current.editor.draft.title, "UNSAVED REVIEW DRAFT");
  assert.equal(current.editor.draft.notes, "Черновик заметки");
  assert.deepEqual(current.editor.draft.ingredients, draft.ingredients);
  assert.deepEqual(current.editor.draft.steps, ["Первый.", "Второй.", "Третий."]);
  assert.equal(env.ctl.hasUnsavedChanges(), true);
  assert.equal(env.net.requests("PATCH", FAV_URL_1).length, 0, "a recheck never saves");
});

scenario("structured_a_draft_is_dropped_when_the_session_ends_or_another_person_signs_in", async () => {
  // The session ends: the editor is gone with everything else in memory.
  const ended = await withOpenStructuredFavorite();
  ended.env.ctl.beginEditFavorite();
  ended.env.ctl.syncFavoriteDraft({ title: "UNSAVED REVIEW DRAFT" });
  ended.env.net.route("GET", "/auth/me", notAuthenticated());
  ended.env.ctl.recheck();
  await ended.env.settle();

  assert.equal(ended.env.view().screen, "login");
  assert.equal(ended.env.ctl._state().favorites.editor, null);
  assert.equal(ended.env.ctl._state().favorites.detail, null);
  assert.ok(!JSON.stringify(ended.env.view()).includes("UNSAVED REVIEW DRAFT"));

  // Another person: the page starts over, and the draft is not theirs.
  const other = await withOpenStructuredFavorite();
  other.env.ctl.beginEditFavorite();
  other.env.ctl.syncFavoriteDraft({ title: "UNSAVED REVIEW DRAFT" });
  borisIsSignedIn(other.env);
  other.env.net.route("GET", "/api/favorites", H.favoritesBody([]));
  other.env.ctl.recheck();
  await other.env.settle();

  const view = other.env.view();
  assert.equal(view.screen, "app");
  assert.equal(view.currentId, CONV_B);
  assert.equal(view.favorites.current, null);
  assert.deepEqual(view.favorites.items, []);
  assert.equal(other.env.ctl._state().favorites.editor, null);
  assert.ok(!JSON.stringify(view).includes("UNSAVED REVIEW DRAFT"));
});

scenario("structured_returning_to_favorites_refreshes_the_open_card_without_losing_a_draft", async () => {
  const { env } = await withOpenStructuredFavorite();
  env.ctl.showSection("chats");
  const changed = structuredFavoriteItem({ title: "Борщ, измененный в другом месте", version: 9 });
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(changed, STRUCTURED_TEXT));

  env.ctl.showSection("favorites");
  await env.settle();
  let current = env.view().favorites.current;
  assert.deepEqual([current.title, current.version], ["Борщ, измененный в другом месте", 9]);

  // With a draft open the background refresh leaves both the card and the draft alone.
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { title: "Мой черновик" });
  env.ctl.updateFavoriteDraft(draft);
  env.ctl.showSection("chats");
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(structuredFavoriteItem({ title: "Еще одно", version: 10 }), STRUCTURED_TEXT));
  env.ctl.showSection("favorites");
  await env.settle();
  current = env.view().favorites.current;
  assert.equal(current.title, "Борщ, измененный в другом месте");
  assert.equal(current.editor.draft.title, "Мой черновик");
});

scenario("structured_returning_to_favorites_refreshes_the_list", async () => {
  const env = makeEnv({ structuredRecipes: true }).signedIn([]);
  env.net.route("GET", FAVORITES_URL, H.favoritesBody([F1]), H.favoritesBody([F2]));
  await env.boot();
  assert.deepEqual(env.view().favorites.items.map((item) => item.title), ["Борщ классический"]);

  env.ctl.showSection("favorites");
  await env.settle();
  assert.deepEqual(env.view().favorites.items.map((item) => item.title), ["Плов с курицей"]);

  env.ctl.showSection("chats");
  await env.ctl.showSection("favorites");
  assert.deepEqual(env.view().favorites.items.map((item) => item.title), ["Плов с курицей"]);
  assert.equal(gets(env, FAVORITES_URL), 3, "every return asks the server again");
});

// ============================================================================================
// A draft is kept exactly as written: no field is ever shortened
// ============================================================================================

// The review's reproduction: a valid step of 1,000 Unicode characters, 500 of them outside
// the basic plane. JavaScript's .length counts 1,500 UTF-16 units for it.
const EMOJI_STEP = "🍅".repeat(500) + "a".repeat(500);

// Mixed scripts: Cyrillic, combining marks, an emoji sequence, a supplementary-plane symbol.
const MIXED_TEXT = "Све́кла 🍅 𝄞 е́ и 👨‍🍳";

scenario("structured_a_step_of_emoji_and_ascii_survives_synchronization_a_recheck_and_saving", async () => {
  const { env } = await withOpenStructuredFavorite({ steps: [EMOJI_STEP, "Добавьте овощи."] });
  assert.equal(env.ctl.beginEditFavorite(), true);

  // What app.js hands over on every keystroke: every field as it now is on the page.
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { title: "Борщ по-домашнему" });
  assert.equal(env.ctl.syncFavoriteDraft(draft), true);

  let current = env.view().favorites.current;
  assert.equal(current.editor.draft.steps[0], EMOJI_STEP);
  assert.equal(Logic.countCodePoints(current.editor.draft.steps[0]), 1000);
  assert.equal(current.editor.draft.steps[0].length, 1500, "the old code cut this at 1,001 UTF-16 units");
  assert.equal(env.net.requests("PATCH", FAV_URL_1).length, 0, "typing saves nothing");

  // A return to the tab checks the session and redraws the editor from the same draft.
  env.ctl.recheck();
  await env.settle();
  current = env.view().favorites.current;
  assert.equal(current.editor.draft.steps[0], EMOJI_STEP);
  assert.equal(env.net.requests("PATCH", FAV_URL_1).length, 0, "a recheck saves nothing");

  const updated = structuredFavoriteItem({
    title: "Борщ по-домашнему",
    steps: [EMOJI_STEP, "Добавьте овощи."],
    version: 4,
  });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.saveFavoriteEdit(current.editor.draft), { ok: true });

  const sent = env.net.requests("PATCH", FAV_URL_1)[0].body;
  assert.equal(sent.steps[0], EMOJI_STEP);
  assert.equal(Logic.countCodePoints(sent.steps[0]), 1000);
  assert.equal(env.net.log.filter((request) => request.method === "POST").length, 0, "no model is called");
});

scenario("structured_editing_only_the_title_keeps_every_other_field_exactly_as_it_was", async () => {
  const ingredients = [
    { name: "Све́кла 🍅", amount: "2 шт. 𝄞" },
    { name: "Лук 👨‍🍳", amount: "1 é" },
  ];
  const steps = ["Обжарьте лук.", MIXED_TEXT];
  const notes = "Заметка с ё и знаками ударения: е́, а́.";
  const servings = "4 порции 🍲";
  const { env } = await withOpenStructuredFavorite({ ingredients, steps, notes, servings });
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { title: "Новый заголовок 🍲" });

  const updated = structuredFavoriteItem({
    title: "Новый заголовок 🍲",
    ingredients,
    steps,
    notes,
    servings,
    version: 4,
  });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: true });

  const sent = env.net.requests("PATCH", FAV_URL_1)[0].body;
  assert.equal(sent.title, "Новый заголовок 🍲");
  assert.equal(sent.servings, servings);
  assert.equal(sent.notes, notes);
  assert.deepEqual(sent.ingredients, ingredients, "every name and amount, in order");
  assert.deepEqual(sent.steps, steps, "every step, in order");
});

scenario("a_legacy_text_of_one_hundred_thousand_characters_survives_synchronization_and_saves_whole", async () => {
  const legacy = "🍅".repeat(50000) + "a".repeat(50000); // 100,000 code points, 150,000 UTF-16 units
  const item = H.favorite(H.FAV_1, 2, "Старый текст", 0, { version: 2 });
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что?"), message(2, "assistant", RECIPE_TEXT)]),
  });
  env.net.route("GET", FAVORITES_URL, H.favoritesBody([item]));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(item, legacy));
  await env.boot();
  await env.ctl.openFavorite(H.FAV_1);
  await env.settle();

  env.ctl.beginEditFavorite();
  assert.equal(env.view().favorites.current.editor.draft.text, legacy);
  assert.equal(Logic.countCodePoints(legacy), 100000);
  assert.equal(legacy.length, 150000, "the old code reduced this to 50,001 characters");

  // The person changes only the title; the text is handed over whole on every keystroke.
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { title: "Новое имя" });
  env.ctl.syncFavoriteDraft(draft);
  env.ctl.recheck();
  await env.settle();
  assert.equal(env.view().favorites.current.editor.draft.text, legacy);
  assert.equal(env.net.requests("PATCH", FAV_URL_1).length, 0, "nothing is saved by a refresh");

  const saved = H.favorite(H.FAV_1, 2, "Новое имя", 0, { version: 3 });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(saved, legacy));
  assert.deepEqual(await env.ctl.saveFavoriteEdit(env.view().favorites.current.editor.draft), { ok: true });

  const sent = env.net.requests("PATCH", FAV_URL_1)[0].body;
  assert.equal(sent.text, legacy);
  assert.equal(sent.title, "Новое имя");
});

scenario("the_maximum_valid_field_lengths_survive_editing_and_saving_a_structured_favorite", async () => {
  // Every field at the server's maximum, measured in code points (outside the basic plane,
  // so each of these is longer in UTF-16 than the limit it is exactly at).
  const title = "🍅".repeat(200);
  const changed = "🍲" + "🍅".repeat(199); // still 200 code points
  const servings = "🍲".repeat(100);
  const ingredients = [{ name: "🍅".repeat(200), amount: "🍅".repeat(100) }];
  const steps = ["🍅".repeat(1000)];
  const notes = "🍅".repeat(2000);
  const { env } = await withOpenStructuredFavorite({ title, servings, ingredients, steps, notes });
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { title: changed });

  const updated = structuredFavoriteItem({ title: changed, servings, ingredients, steps, notes, version: 4 });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: true });

  const sent = env.net.requests("PATCH", FAV_URL_1)[0].body;
  assert.equal(sent.title, changed);
  assert.equal(sent.servings, servings);
  assert.deepEqual(sent.ingredients, ingredients);
  assert.deepEqual(sent.steps, steps);
  assert.equal(sent.notes, notes);
});

scenario("an_over_limit_structured_field_is_refused_with_a_reason_and_the_whole_draft_stays", async () => {
  const cases = [
    { change: { title: "я".repeat(201) }, message: Logic.TEXT.editTitleTooLong },
    { change: { notes: "😀".repeat(2001) }, message: Logic.TEXT.editNotesTooLong },
    { change: { servings: "🍲".repeat(101) }, message: Logic.TEXT.editServingsTooLong },
    {
      change: { ingredients: [{ name: "🍅".repeat(201), amount: "1" }] },
      message: Logic.TEXT.editIngredientNameTooLong,
    },
    {
      change: { ingredients: [{ name: "свекла", amount: "🍅".repeat(101) }] },
      message: Logic.TEXT.editIngredientAmountTooLong,
    },
    { change: { steps: ["🍅".repeat(1001)] }, message: Logic.TEXT.editStepTooLong },
    {
      change: { ingredients: Array.from({ length: 101 }, () => ({ name: "a", amount: "1" })) },
      message: Logic.TEXT.editTooManyIngredients,
    },
    {
      change: { steps: Array.from({ length: 51 }, (_, index) => "Шаг " + index) },
      message: Logic.TEXT.editTooManySteps,
    },
  ];
  for (const entry of cases) {
    const { env } = await withOpenStructuredFavorite();
    env.ctl.beginEditFavorite();
    const draft = Object.assign({}, env.view().favorites.current.editor.draft, entry.change);

    assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: false, reason: "invalid" });
    const current = env.view().favorites.current;
    assert.equal(current.editor.phase, "editing");
    assert.equal(current.editor.error.text, entry.message);
    assert.deepEqual(current.editor.draft, draft, "the whole draft, byte for character");
    assert.equal(env.net.requests("PATCH", FAV_URL_1).length, 0, "nothing over the limit is sent");
  }
});

scenario("an_over_limit_legacy_text_is_refused_with_a_reason_and_never_shortened", async () => {
  const stored = "🍅".repeat(100000); // exactly the server's maximum
  const item = H.favorite(H.FAV_1, 2, "Старый текст", 0, { version: 2 });
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что?"), message(2, "assistant", RECIPE_TEXT)]),
  });
  env.net.route("GET", FAVORITES_URL, H.favoritesBody([item]));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(item, stored));
  await env.boot();
  await env.ctl.openFavorite(H.FAV_1);
  await env.settle();
  env.ctl.beginEditFavorite();

  const tooLong = stored + "🍅"; // 100,001 code points
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { text: tooLong });
  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: false, reason: "invalid" });

  let current = env.view().favorites.current;
  assert.equal(current.editor.error.text, Logic.TEXT.editTextTooLong);
  assert.equal(current.editor.draft.text, tooLong, "the entire draft is kept");
  assert.equal(env.net.requests("PATCH", FAV_URL_1).length, 0);

  // Nothing was lost: removing the extra character lets the same draft save.
  const saved = H.favorite(H.FAV_1, 2, "Старый текст", 0, { version: 3 });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(saved, stored));
  assert.deepEqual(
    await env.ctl.saveFavoriteEdit(Object.assign({}, current.editor.draft, { text: stored })),
    { ok: true }
  );
  assert.equal(env.net.requests("PATCH", FAV_URL_1)[0].body.text, stored);
});

// ============================================================================================
// The Telegram panel: connecting an account (createTelegramLink)
// ============================================================================================

const BOT = "house_helper_app_bot";
const CODE = "k3JmQ0v9aZxP4tR7uYw2LbN6cD8eFgHs";
const PAIR_LINK = `https://t.me/${BOT}?start=${CODE}`;
const status = (linked, extra = {}) => json(200, linked
  ? { enabled: true, linked: true, bot_username: BOT, account: { name: "@mama", linked_at: "2026-10-18T12:00:00+00:00" }, ...extra }
  : { enabled: true, linked: false, bot_username: BOT, ...extra });
const issued = (overrides = {}) =>
  json(201, { code: CODE, link: PAIR_LINK, expires_at: "2026-10-18T12:10:00+00:00", expires_in_seconds: 600, ...overrides });

function telegramPanel(routes = {}) {
  const net = new H.Net();
  const timers = new H.Timers();
  H.environments.push({ net });
  for (const [key, responders] of Object.entries(routes)) {
    const [method, path] = key.split(" ");
    net.route(method, path, ...responders);
  }
  const changes = { count: 0 };
  const panel = Logic.createTelegramLink({
    fetch: net.fetch,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    now: () => timers.now,
    AbortController,
    onChange: () => {
      changes.count++;
    },
  });
  return { panel, net, timers, changes, view: () => panel.getView() };
}

scenario("telegram_panel_is_invisible_until_started_and_when_the_bot_is_not_here", async () => {
  const t = telegramPanel({ "GET /api/telegram": [json(200, { enabled: false, linked: false })] });
  assert.equal(t.view().visible, false);
  await t.panel.start();
  assert.equal(t.view().visible, false);
  assert.equal(t.view().text, "");
  assert.deepEqual(t.net.requests("POST", "/api/telegram/link"), []);
});

scenario("telegram_panel_offers_to_connect_and_says_what_it_gives", async () => {
  const t = telegramPanel({ "GET /api/telegram": [status(false)] });
  await t.panel.start();
  const view = t.view();
  assert.equal(view.visible, true);
  assert.equal(view.phase, "unlinked");
  assert.equal(view.canConnect, true);
  assert.equal(view.canAskDisconnect, false);
  assert.equal(view.link, null);
  assert.ok(/общими с сайтом/.test(view.text));
  assert.equal(view.notice, null, "a bot that works has nothing to apologise for");
  const request = t.net.requests("GET", "/api/telegram")[0];
  assert.equal(request.init.credentials, "same-origin");
  assert.equal(request.init.cache, "no-store");
  assert.equal(request.headers.cookie, undefined);
  assert.equal(request.body, undefined);
});

scenario("telegram_panel_always_says_what_is_copied_where_it_is_processed_and_how_long_it_is_kept", async () => {
  const idle = telegramPanel({ "GET /api/telegram": [json(200, { enabled: false, linked: false })] });
  await idle.panel.start();
  assert.equal(idle.view().privacy, "", "nothing is said where there is no panel");

  for (const reply of [status(false), status(true)]) {
    const t = telegramPanel({ "GET /api/telegram": [reply] });
    const before = t.view();
    assert.equal(before.privacy, "");
    await t.panel.start();
    const privacy = t.view().privacy;
    assert.ok(privacy.includes("дублируется в этот Telegram-чат"), "the copy to Telegram is explained");
    assert.ok(privacy.includes("только отключением Telegram"), "and how to end it");
    assert.ok(privacy.includes("OpenAI"), "messages and photos go to OpenAI");
    assert.ok(privacy.includes("фотографии"));
    assert.ok(privacy.includes("365 дней"), "history expires");
    assert.ok(privacy.includes("в избранное") && privacy.includes("пока вы сами их не уберете"), "favorites do not");
  }
});

scenario("telegram_panel_says_when_the_bot_is_not_answering_and_offers_nothing_it_cannot_do", async () => {
  const down = { available: false };
  const t = telegramPanel({ "GET /api/telegram": [status(false, down), status(false)] });
  await t.panel.start();
  let view = t.view();
  assert.equal(view.visible, true);
  assert.equal(view.notice, Logic.TELEGRAM_TEXT.unavailable);
  assert.equal(view.canConnect, false, "no code is asked for from a bot that is not reading");
  assert.deepEqual(t.net.requests("POST", "/api/telegram/link"), []);
  await t.panel.recheck();
  view = t.view();
  assert.equal(view.notice, null, "and the notice goes when the bot is back");
  assert.equal(view.canConnect, true);

  const linked = telegramPanel({ "GET /api/telegram": [status(true, down)] });
  await linked.panel.start();
  view = linked.view();
  assert.equal(view.text, "Подключен: @mama", "an account that is linked stays linked");
  assert.equal(view.notice, Logic.TELEGRAM_TEXT.unavailable, "but is not shown as working");
  assert.equal(view.canAskDisconnect, true, "and can still be unlinked");

  const odd = telegramPanel({ "GET /api/telegram": [status(false, { available: "no" })] });
  await odd.panel.start();
  assert.equal(odd.view().canConnect, true, "only an explicit false means the bot is down");
});

scenario("telegram_panel_pairing_shows_the_link_and_the_command_then_notices_the_pairing_by_itself", async () => {
  const t = telegramPanel({
    "GET /api/telegram": [status(false), status(false), status(false), status(true)],
    "POST /api/telegram/link": [issued()],
  });
  await t.panel.start();
  await t.panel.connect();
  let view = t.view();
  assert.equal(view.phase, "pairing");
  assert.equal(view.link, PAIR_LINK);
  assert.ok(view.text.includes(`/start ${CODE}`) && view.text.includes("@" + BOT), view.text);
  assert.ok(view.text.includes("10 минут"));
  assert.equal(view.canConnect, false);
  assert.deepEqual(t.net.requests("POST", "/api/telegram/link")[0].body, {});
  assert.deepEqual(t.timers.delays, [3000]);

  await t.timers.advance(3000);
  await flush();
  await t.timers.advance(3000);
  await flush();
  assert.equal(t.view().phase, "pairing", "not yet");
  await t.timers.advance(3000);
  await flush();
  view = t.view();
  assert.equal(view.phase, "linked");
  assert.equal(view.link, null, "the link is gone once it has been used");
  assert.ok(!view.text.includes(CODE));
  assert.equal(view.notice, Logic.TELEGRAM_TEXT.pairedNow);
  assert.equal(t.timers.pending, 0, "polling stopped");
});

scenario("telegram_panel_an_unused_code_expires_and_nothing_of_it_stays", async () => {
  const t = telegramPanel({
    "GET /api/telegram": [status(false)],
    "POST /api/telegram/link": [issued({ expires_in_seconds: 10 })],
  });
  await t.panel.start();
  await t.panel.connect();
  for (let i = 0; i < 5; i++) {
    await t.timers.advance(3000);
    await flush();
  }
  const view = t.view();
  assert.equal(view.phase, "unlinked");
  assert.equal(view.link, null);
  assert.equal(view.notice, Logic.TELEGRAM_TEXT.expired);
  assert.ok(!JSON.stringify(view).includes(CODE));
  assert.equal(t.timers.pending, 0);
});

scenario("telegram_panel_refuses_a_link_that_is_not_the_bots", async () => {
  const hostile = [
    "javascript:alert(1)",
    "https://evil.example/house_helper_app_bot?start=" + CODE,
    "http://t.me/house_helper_app_bot?start=" + CODE,
    "https://t.me.evil.example/b_bot?start=" + CODE,
    `https://t.me/${BOT}?start=${CODE}&x=1`,
    `https://t.me/${BOT}/../x?start=${CODE}`,
    `https://t.me/${BOT}?start=<script>`,
    `//t.me/${BOT}?start=${CODE}`,
    "",
    null,
    12,
  ];
  for (const link of hostile) {
    const t = telegramPanel({ "GET /api/telegram": [status(false)], "POST /api/telegram/link": [issued({ link })] });
    await t.panel.start();
    await t.panel.connect();
    assert.equal(t.view().link, null, String(link));
    assert.notEqual(t.view().phase, "pairing", String(link));
    assert.equal(t.timers.pending, 0);
  }
  for (const code of ["short", "has space 1234567890abcdef", "<b>12345678901234567890</b>", 5, null]) {
    const t = telegramPanel({ "GET /api/telegram": [status(false)], "POST /api/telegram/link": [issued({ code })] });
    await t.panel.start();
    await t.panel.connect();
    assert.notEqual(t.view().phase, "pairing", String(code));
  }
});

scenario("telegram_panel_linked_shows_the_account_and_unlinking_asks_first", async () => {
  const t = telegramPanel({
    "GET /api/telegram": [status(true)],
    "DELETE /api/telegram/link": [json(200, { linked: false })],
  });
  await t.panel.start();
  let view = t.view();
  assert.equal(view.phase, "linked");
  assert.equal(view.text, "Подключен: @mama");
  assert.equal(view.canAskDisconnect, true);
  await t.panel.disconnect(); // without asking: nothing happens
  assert.deepEqual(t.net.requests("DELETE", "/api/telegram/link"), []);

  t.panel.askDisconnect();
  view = t.view();
  assert.equal(view.text, Logic.TELEGRAM_TEXT.askUnlink);
  assert.equal(view.canConfirmDisconnect, true);
  assert.equal(view.canCancel, true);
  t.panel.cancelDisconnect();
  assert.equal(t.view().canAskDisconnect, true);
  assert.deepEqual(t.net.requests("DELETE", "/api/telegram/link"), []);

  t.panel.askDisconnect();
  await t.panel.disconnect();
  view = t.view();
  assert.equal(view.phase, "unlinked");
  assert.equal(view.notice, Logic.TELEGRAM_TEXT.unlinkedNow);
  assert.equal(t.net.requests("DELETE", "/api/telegram/link").length, 1);
});

scenario("telegram_panel_a_failed_unlinking_keeps_the_account_and_says_why", async () => {
  const t = telegramPanel({
    "GET /api/telegram": [status(true)],
    "DELETE /api/telegram/link": [apiError(503, "service_unavailable", { retryable: true })],
  });
  await t.panel.start();
  t.panel.askDisconnect();
  await t.panel.disconnect();
  const view = t.view();
  assert.equal(view.phase, "linked");
  assert.equal(view.notice, Logic.CODE_MESSAGES.service_unavailable);
});

scenario("telegram_panel_an_account_already_linked_in_another_tab_shows_as_linked", async () => {
  const t = telegramPanel({
    "GET /api/telegram": [status(false), status(true)],
    "POST /api/telegram/link": [apiError(409, "telegram_already_linked")],
  });
  await t.panel.start();
  await t.panel.connect();
  assert.equal(t.view().phase, "linked");
  assert.equal(t.timers.pending, 0);
});

scenario("telegram_panel_failures_are_said_in_russian_and_can_be_retried", async () => {
  const t = telegramPanel({ "GET /api/telegram": [fail(), apiError(503, "service_unavailable", { retryable: true }), apiError(503, "service_unavailable", { retryable: true }), status(false)] });
  await t.panel.start();
  assert.equal(t.view().phase, "error");
  assert.equal(t.view().notice, Logic.TEXT.network);
  assert.equal(t.view().retry, true);
  assert.equal(t.view().canConnect, true);
  await t.panel.connect(); // the button of a failure says "Повторить": it reads again and asks for no code
  assert.deepEqual(t.net.requests("POST", "/api/telegram/link"), []);
  assert.equal(t.view().notice, Logic.CODE_MESSAGES.service_unavailable);
  await t.panel.recheck();
  assert.equal(t.view().notice, Logic.CODE_MESSAGES.service_unavailable);
  await t.panel.recheck();
  assert.equal(t.view().phase, "unlinked");
  assert.equal(t.view().notice, null);

  const other = telegramPanel({ "GET /api/telegram": [text(502, "<html>bad gateway</html>")] });
  await other.panel.start();
  assert.equal(other.view().phase, "error");
  assert.equal(other.view().notice, Logic.TELEGRAM_TEXT.failed);

  const odd = telegramPanel({ "GET /api/telegram": [json(200, { enabled: "yes", linked: 1 })] });
  await odd.panel.start();
  assert.equal(odd.view().phase, "error");
});

scenario("telegram_panel_a_request_that_fails_while_pairing_does_not_lose_the_code", async () => {
  const t = telegramPanel({
    "GET /api/telegram": [status(false), fail(), status(true)],
    "POST /api/telegram/link": [issued()],
  });
  await t.panel.start();
  await t.panel.connect();
  await t.timers.advance(3000);
  await flush();
  assert.equal(t.view().phase, "pairing");
  assert.equal(t.view().link, PAIR_LINK);
  await t.timers.advance(3000);
  await flush();
  assert.equal(t.view().phase, "linked");
});

scenario("telegram_panel_a_lost_session_hides_the_panel_without_a_word", async () => {
  const t = telegramPanel({ "GET /api/telegram": [status(false), notAuthenticated()], "POST /api/telegram/link": [issued()] });
  await t.panel.start();
  await t.panel.connect();
  await t.timers.advance(3000);
  await flush();
  const view = t.view();
  assert.equal(view.visible, false);
  assert.ok(!JSON.stringify(view).includes(CODE));
  assert.equal(t.timers.pending, 0);
});

scenario("telegram_panel_stopping_forgets_everything_and_ignores_late_answers", async () => {
  const gate = deferred();
  const t = telegramPanel({ "GET /api/telegram": [status(true, { account: { name: "@mama" } }), async () => { await gate.promise; return status(true); }] });
  await t.panel.start();
  assert.equal(t.view().phase, "linked");
  const late = t.panel.recheck();
  t.panel.stop();
  assert.equal(t.view().visible, false);
  gate.resolve();
  await late;
  await flush();
  const view = t.view();
  assert.equal(view.visible, false, "the late answer was not applied");
  assert.ok(!JSON.stringify(view).includes("@mama"));
  // and a new start is a fresh one
  const again = telegramPanel({ "GET /api/telegram": [status(false)] });
  await again.panel.start();
  again.panel.stop();
  await again.panel.start();
  assert.equal(again.view().phase, "unlinked");
});

scenario("telegram_panel_does_not_ask_twice_for_a_code_at_once", async () => {
  const gate = deferred();
  const t = telegramPanel({
    "GET /api/telegram": [status(false)],
    "POST /api/telegram/link": [async () => { await gate.promise; return issued(); }],
  });
  await t.panel.start();
  const first = t.panel.connect();
  const second = t.panel.connect();
  assert.equal(t.view().busy, true);
  assert.equal(t.view().canConnect, false);
  gate.resolve();
  await Promise.all([first, second]);
  assert.equal(t.net.requests("POST", "/api/telegram/link").length, 1);
});

scenario("telegram_panel_text_is_russian_and_holds_only_what_the_server_said", async () => {
  const latin = /[A-Za-z]/;
  for (const [name, value] of Object.entries(Logic.TELEGRAM_TEXT)) {
    const shown = value.replace(/\{\w+\}/g, "").replace(/Telegram|OpenAI|\/start|Start/g, "");
    assert.ok(/[А-Яа-яЁё]/.test(shown), `${name} has no Russian`);
    assert.ok(!latin.test(shown), `${name} has Latin letters: ${shown}`);
  }
  const t = telegramPanel({ "GET /api/telegram": [json(200, { enabled: true, linked: true, bot_username: BOT, account: { name: "<img src=x onerror=alert(1)>" } })] });
  await t.panel.start();
  assert.equal(t.view().text, "Подключен: <img src=x onerror=alert(1)>", "the name is data: the page draws it as text");
});

scenario("telegram_panel_refreshes_when_the_page_comes_back_and_never_when_nothing_is_shown", async () => {
  const t = telegramPanel({ "GET /api/telegram": [status(false), status(true)] });
  await t.panel.recheck();
  assert.deepEqual(t.net.requests("GET", "/api/telegram"), [], "not started: nothing is asked");
  await t.panel.start();
  await t.panel.recheck();
  assert.equal(t.view().phase, "linked");
});

// ============================================================================================
// A photo that came with a message
// ============================================================================================

const PHOTO_ID = "5d6c1f3e-0a9b-4e43-9c2f-7b1a8d3e4f50";
const photoOf = (url, type = "image/jpeg") => [{ id: PHOTO_ID, content_type: type, size: 1234, url }];

scenario("a_photo_with_a_message_is_kept_as_an_address_on_this_site_and_nothing_else", async () => {
  const url = `/api/attachments/${PHOTO_ID}`;
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [
      message(1, "user", "Что это?", { attachments: photoOf(url) }),
      message(2, "assistant", "Это кастрюля."),
      message(3, "user", "Ещё", { attachments: photoOf(url, "image/png") }),
    ]),
  });
  await env.boot();
  const items = env.view().thread.items;
  assert.deepEqual(items.map((i) => i.photo), [{ url }, null, { url }]);
  assert.ok(!JSON.stringify(items).includes("data:"));
});

scenario("a_photo_address_that_is_not_the_servers_own_route_is_not_a_photo", async () => {
  const hostile = [
    "javascript:alert(1)",
    "https://evil.example/x.png",
    "//evil.example/api/attachments/" + PHOTO_ID,
    "/api/attachments/../../auth/me",
    "/api/attachments/" + PHOTO_ID + "?x=1",
    "/api/attachments/" + PHOTO_ID + "/",
    "/api/attachments/not-a-uuid",
    "/static/app.js",
    "data:image/png;base64,AAAA",
    "",
    null,
    42,
  ];
  for (const url of hostile) {
    const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
      [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что это?", { attachments: photoOf(url) })]),
    });
    await env.boot();
    assert.equal(env.view().thread.items[0].photo, null, String(url));
    assert.equal(env.view().thread.items[0].text, "Что это?");
  }
  const url = `/api/attachments/${PHOTO_ID}`;
  for (const type of ["image/gif", "text/html", "image/svg+xml", "", null, 5]) {
    const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
      [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что это?", { attachments: photoOf(url, type) })]),
    });
    await env.boot();
    assert.equal(env.view().thread.items[0].photo, null, String(type));
  }
  for (const list of [null, "x", {}, [], [null], [{}], [{ url }]]) {
    const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
      [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что это?", { attachments: list })]),
    });
    await env.boot();
    assert.equal(env.view().thread.items[0].photo, null, JSON.stringify(list));
  }
});

// ============================================================================================
// Deleting a conversation
// ============================================================================================

scenario("deleting_a_conversation_is_asked_first_and_the_open_one_becomes_a_new_draft", async () => {
  const env = await withConversation();
  // An unresolved turn, so its stored record can be seen to go with the conversation.
  env.net.route("POST", URL_A, apiError(502, "provider_error", { retryable: true }));
  const sent = await env.ctl.send("Не потеряй");
  await sent.done;
  await env.settle();
  assert.equal(env.storage.record.turns.length, 1);
  env.net.route("DELETE", `/api/conversations/${CONV_A}`, H.removedBody());
  // What the server lists after the deletion: the list is read again, and it is empty.
  env.net.route("GET", "/api/conversations", listBody([]));

  env.ctl.askDeleteConversation(CONV_A);
  await env.settle();
  let view = env.view();
  assert.deepEqual([view.list.deleting.id, view.list.deleting.phase], [CONV_A, "asking"]);
  assert.equal(env.net.requests("DELETE", /.*/).length, 0, "asking removes nothing");

  env.ctl.cancelDeleteConversation();
  await env.settle();
  assert.equal(env.view().list.deleting, null);
  assert.equal(env.net.requests("DELETE", /.*/).length, 0);

  env.ctl.askDeleteConversation(CONV_A);
  const result = await env.ctl.deleteConversation();
  await env.settle();
  view = env.view();

  assert.deepEqual([result.ok, result.wasCurrent], [true, true]);
  assert.equal(env.net.requests("DELETE", /.*/).length, 1);
  assert.equal(env.net.requests("GET", "/api/conversations").length, 2, "the list is read again after the deletion");
  assert.deepEqual([view.currentId, view.list.items.length, view.thread.items.length], [null, 0, 0]);
  assert.equal(view.list.status, "ready");
  assert.equal(view.chatTitle, "Новый разговор");
  assert.deepEqual(view.banner, { kind: "info", text: Logic.TEXT.deleted, correlationId: null });
  assert.deepEqual(env.ctl._state().pending, {}, "the unresolved turn goes with the conversation");
  assert.equal(env.storage.record, null);
  assert.deepEqual(env.announced, [Logic.TEXT.deleted]);
});

scenario("deleting_another_conversation_keeps_the_open_one_and_removes_only_it", async () => {
  const env = makeEnv().signedIn([conversation(CONV_B, 30), conversation(CONV_A, 0)], {
    [CONV_B]: historyBody(CONV_B, [message(5, "user", "Про суп"), message(6, "assistant", "Суп так")]),
  });
  await env.boot();
  assert.equal(env.view().currentId, CONV_B);
  env.net.route("DELETE", `/api/conversations/${CONV_A}`, H.removedBody());
  env.net.route("GET", "/api/conversations", listBody([conversation(CONV_B, 30)]));

  env.ctl.askDeleteConversation(CONV_A);
  const result = await env.ctl.deleteConversation();
  await env.settle();
  const view = env.view();

  assert.deepEqual([result.ok, result.wasCurrent], [true, false]);
  assert.equal(view.currentId, CONV_B);
  assert.deepEqual(view.list.items.map((item) => item.id), [CONV_B]);
  assert.equal(view.list.status, "ready");
  assert.deepEqual(view.thread.items.map((item) => item.text), ["Про суп", "Суп так"]);
  assert.equal(view.banner, null);
  assert.equal(view.list.deleting, null);
});

scenario("a_conversation_deleted_elsewhere_is_dropped_as_the_outcome_that_was_wanted", async () => {
  const env = await withConversation();
  env.net.route("DELETE", `/api/conversations/${CONV_A}`, apiError(404, "conversation_not_found"));
  env.net.route("GET", "/api/conversations", listBody([]));

  env.ctl.askDeleteConversation(CONV_A);
  const result = await env.ctl.deleteConversation();
  await env.settle();
  const view = env.view();

  assert.deepEqual([result.ok, result.wasCurrent], [true, true]);
  assert.deepEqual([view.currentId, view.list.items.length], [null, 0]);
  assert.equal(view.list.status, "ready");
  assert.equal(env.net.requests("GET", "/api/conversations").length, 2, "a 404 is a confirmed deletion too");
  assert.deepEqual(view.banner, { kind: "info", text: Logic.TEXT.deleted, correlationId: null });
});

scenario("a_failed_deletion_keeps_the_conversation_and_can_be_retried", async () => {
  const env = await withConversation();
  const failure = apiError(503, "service_unavailable", { retryable: true });
  env.net.route("DELETE", `/api/conversations/${CONV_A}`, failure, H.removedBody());

  env.ctl.askDeleteConversation(CONV_A);
  const failed = await env.ctl.deleteConversation();
  await env.settle();
  let view = env.view();

  assert.equal(failed.ok, false);
  assert.equal(view.currentId, CONV_A);
  assert.equal(view.list.items.length, 1);
  assert.equal(view.list.deleting.phase, "error");
  assert.equal(view.list.deleting.error.text, Logic.CODE_MESSAGES.service_unavailable);
  assert.equal(view.list.deleting.error.correlationId, failure.headers["X-Correlation-ID"]);
  assert.equal(view.banner, null);
  assert.equal(env.net.requests("GET", "/api/conversations").length, 1, "a failed deletion refreshes nothing");

  env.net.route("GET", "/api/conversations", listBody([]));
  const retried = await env.ctl.deleteConversation();
  await env.settle();
  view = env.view();
  assert.equal(retried.ok, true);
  assert.deepEqual([view.currentId, view.list.items.length], [null, 0]);
  assert.equal(view.list.status, "ready");
  assert.equal(env.net.requests("GET", "/api/conversations").length, 2);
});

scenario("deleting_while_the_list_is_loading_ignores_the_held_pre_deletion_answer", async () => {
  const env = await withConversation();
  const staleReached = deferred();
  const releaseStale = deferred();
  const freshReached = deferred();
  const releaseFresh = deferred();
  // The list is read (this answer still has the conversation), then, after the deletion, read
  // again. Both answers are held, so their order can be controlled.
  env.net.route(
    "GET",
    "/api/conversations",
    async () => {
      staleReached.resolve();
      await releaseStale.promise;
      return listBody([conversation(CONV_A, 0)]);
    },
    async () => {
      freshReached.resolve();
      await releaseFresh.promise;
      return listBody([]);
    }
  );
  const listing = env.ctl.reloadList();
  await staleReached.promise;
  assert.equal(env.view().list.status, "loading");

  env.net.route("DELETE", `/api/conversations/${CONV_A}`, H.removedBody());
  env.ctl.askDeleteConversation(CONV_A);
  const result = await env.ctl.deleteConversation();
  await freshReached.promise;

  assert.deepEqual([result.ok, result.wasCurrent], [true, true]);
  assert.equal(
    env.net.requests("GET", "/api/conversations").length,
    3,
    "the boot read, the held one, and the fresh one the deletion starts"
  );
  assert.deepEqual(env.view().list.items, [], "the conversation is gone from the list at once");
  assert.equal(env.view().list.status, "loading", "an invalidated list is not called ready");
  assert.equal(env.view().currentId, null);

  releaseFresh.resolve();
  await flush();
  assert.equal(env.view().list.status, "ready", "the fresh answer makes the list authoritative");
  assert.deepEqual(env.view().list.items, []);

  releaseStale.resolve();
  await listing;
  await env.settle();

  const view = env.view();
  assert.deepEqual(view.list.items.map((item) => item.id), [], "the held answer must not restore it");
  assert.equal(view.list.status, "ready");
  assert.equal(view.currentId, null);
  assert.deepEqual(view.banner, { kind: "info", text: Logic.TEXT.deleted, correlationId: null });
});

scenario("a_successful_deletion_reads_the_list_again_from_the_server", async () => {
  const env = await withConversation();
  const freshReached = deferred();
  const releaseFresh = deferred();
  env.net.route("DELETE", `/api/conversations/${CONV_A}`, H.removedBody());
  env.net.route("GET", "/api/conversations", async () => {
    freshReached.resolve();
    await releaseFresh.promise;
    return listBody([conversation(CONV_B, 10)]);
  });
  env.ctl.askDeleteConversation(CONV_A);
  const result = await env.ctl.deleteConversation();
  await freshReached.promise;

  assert.equal(result.ok, true);
  assert.equal(env.net.requests("GET", "/api/conversations").length, 2, "a fresh read follows the DELETE");
  assert.equal(env.view().list.status, "loading");
  assert.deepEqual(env.view().list.items, []);

  releaseFresh.resolve();
  await env.settle();

  const view = env.view();
  assert.deepEqual(view.list.items.map((item) => item.id), [CONV_B]);
  assert.equal(view.list.status, "ready");
  assert.equal(view.currentId, null, "deleting the open conversation leaves a new draft");
});

scenario("a_deletion_confirmed_while_held_back_reads_the_list_once_the_person_is_confirmed", async () => {
  const env = await withConversation();
  const deleteReached = deferred();
  const releaseDelete = deferred();
  const verifyReached = deferred();
  const releaseVerify = deferred();
  env.net.route("DELETE", `/api/conversations/${CONV_A}`, async () => {
    deleteReached.resolve();
    await releaseDelete.promise;
    return H.removedBody();
  });
  env.net.route("GET", "/api/conversations", listBody([]));

  env.ctl.askDeleteConversation(CONV_A);
  const deleting = env.ctl.deleteConversation();
  await deleteReached.promise;

  // The page is held back while the deletion is being confirmed.
  env.net.route("GET", "/auth/me", async () => {
    verifyReached.resolve();
    await releaseVerify.promise;
    return me();
  });
  env.ctl.recheck();
  await verifyReached.promise;
  assert.equal(env.ctl._state().gate.phase, "checking");

  releaseDelete.resolve();
  const result = await deleting;
  assert.equal(result.ok, true);
  assert.equal(env.net.requests("GET", "/api/conversations").length, 1, "nothing is read while the page is held back");
  assert.deepEqual(env.ctl._state().list.items, [], "the conversation is gone from memory at once");

  releaseVerify.resolve(); // the same person: the page is let through, and the list is read
  await env.settle();

  assert.equal(env.net.requests("GET", "/api/conversations").length, 2);
  const view = env.view();
  assert.equal(view.list.status, "ready");
  assert.deepEqual(view.list.items, []);
  assert.equal(view.currentId, null);
});

scenario("deleting_a_visible_conversation_reveals_the_one_beyond_the_page", async () => {
  // 51 conversations, of which the page shows the newest 50. Deleting one of the visible ones
  // must let the server fill the page back up: the 51st appears, without a manual reload.
  const ids = Array.from(
    { length: 51 },
    (_, i) => `dddddddd-dddd-4ddd-8ddd-${String(i).padStart(12, "0")}`
  );
  const all = ids.map((id, i) => conversation(id, 100 - i)); // newest first
  const histories = {};
  for (const item of all.slice(0, Logic.PAGE_SIZE)) {
    histories[item.id] = historyBody(item.id, []);
  }
  const env = makeEnv().signedIn(all.slice(0, Logic.PAGE_SIZE), histories);
  env.net.route("GET", "/api/conversations", listBody(all.slice(0, Logic.PAGE_SIZE), true));
  await env.boot();
  assert.deepEqual(
    [env.view().list.items.length, env.view().list.hasMore],
    [Logic.PAGE_SIZE, true],
    "the page shows the newest 50 and knows there are more"
  );

  const deleted = all[0];
  assert.equal(env.view().currentId, deleted.id);
  env.net.route("DELETE", `/api/conversations/${deleted.id}`, H.removedBody());
  env.net.route("GET", "/api/conversations", listBody(all.slice(1), false));

  env.ctl.askDeleteConversation(deleted.id);
  const result = await env.ctl.deleteConversation();
  await env.settle();

  const view = env.view();
  assert.deepEqual([result.ok, result.wasCurrent], [true, true]);
  assert.equal(
    env.net.requests("GET", "/api/conversations").length,
    2,
    "the refreshed page needed no manual reload"
  );
  assert.equal(view.list.status, "ready");
  assert.equal(view.list.items.length, Logic.PAGE_SIZE);
  assert.equal(view.list.items.some((item) => item.id === deleted.id), false, "the deleted one is not back");
  assert.equal(view.list.items[Logic.PAGE_SIZE - 1].id, all[50].id, "the previously hidden conversation is shown");
  assert.equal(view.list.hasMore, false);
  assert.equal(view.currentId, null);
});

scenario("a_failed_post_deletion_refresh_is_recoverable_and_never_restores_the_conversation", async () => {
  const env = await withConversation();
  env.net.route("DELETE", `/api/conversations/${CONV_A}`, H.removedBody());
  // The fresh read fails; the retry (the list's own retry) succeeds with the server's list.
  env.net.route("GET", "/api/conversations", fail(), listBody([]));

  env.ctl.askDeleteConversation(CONV_A);
  const result = await env.ctl.deleteConversation();
  await env.settle();
  let view = env.view();

  assert.equal(result.ok, true, "the deletion itself succeeded");
  assert.equal(view.list.status, "error");
  assert.equal(view.list.error.text, Logic.TEXT.network);
  assert.deepEqual(view.list.items, [], "the confirmed-deleted conversation is not put back by the failure");
  assert.equal(view.currentId, null);
  assert.deepEqual(view.banner, { kind: "info", text: Logic.TEXT.deleted, correlationId: null });

  await env.ctl.reloadList();
  await env.settle();
  view = env.view();
  assert.equal(view.list.status, "ready");
  assert.deepEqual(view.list.items, []);
  assert.equal(env.net.requests("GET", "/api/conversations").length, 3);
});

scenario("a_deletion_rejected_as_signed_out_keeps_the_session_flow_and_reads_no_list", async () => {
  const env = await withConversation();
  env.net.route("DELETE", `/api/conversations/${CONV_A}`, notAuthenticated());

  env.ctl.askDeleteConversation(CONV_A);
  const result = await env.ctl.deleteConversation();
  await env.settle();

  assert.deepEqual([result.ok, result.reason], [false, "auth"]);
  assert.equal(env.view().screen, "login");
  assert.equal(env.net.requests("GET", "/api/conversations").length, 1, "a rejected deletion refreshes nothing");
});

scenario("an_older_post_deletion_list_answer_does_not_replace_a_newer_one", async () => {
  const env = await withConversation();
  const olderReached = deferred();
  const releaseOlder = deferred();
  env.net.route(
    "GET",
    "/api/conversations",
    async () => {
      olderReached.resolve();
      await releaseOlder.promise;
      return listBody([conversation(CONV_A, 0)]); // the state before the deletion
    },
    listBody([conversation(CONV_B, 10)])
  );
  env.net.route("DELETE", `/api/conversations/${CONV_A}`, H.removedBody());
  env.ctl.askDeleteConversation(CONV_A);
  const result = await env.ctl.deleteConversation();
  await olderReached.promise;
  assert.equal(result.ok, true);

  const newer = env.ctl.reloadList(); // started after the deletion's own read, and answered first
  await newer;
  await flush();
  assert.deepEqual(env.view().list.items.map((item) => item.id), [CONV_B]);

  releaseOlder.resolve();
  await env.settle();

  const view = env.view();
  assert.deepEqual(view.list.items.map((item) => item.id), [CONV_B], "the older answer is ignored");
  assert.equal(view.list.items.some((item) => item.id === CONV_A), false);
  assert.equal(view.list.status, "ready");
});

scenario("calling_off_a_deletion_does_not_discard_a_list_answer_on_its_way", async () => {
  const env = await withConversation();
  const reached = deferred();
  const release = deferred();
  env.net.route("GET", "/api/conversations", async () => {
    reached.resolve();
    await release.promise;
    return listBody([conversation(CONV_A, 5, "Новое имя")]);
  });
  const listing = env.ctl.reloadList();
  await reached.promise;

  env.ctl.askDeleteConversation(CONV_A);
  env.ctl.cancelDeleteConversation();
  release.resolve();
  await listing;
  await env.settle();

  const view = env.view();
  assert.deepEqual(view.list.items.map((item) => item.label), ["Новое имя"]);
  assert.equal(view.list.status, "ready");
  assert.equal(view.currentId, CONV_A);
});

scenario("a_failed_deletion_does_not_discard_a_list_answer_on_its_way", async () => {
  const env = await withConversation();
  const reached = deferred();
  const release = deferred();
  env.net.route("GET", "/api/conversations", async () => {
    reached.resolve();
    await release.promise;
    return listBody([conversation(CONV_A, 5, "Новое имя")]);
  });
  const listing = env.ctl.reloadList();
  await reached.promise;

  env.net.route("DELETE", `/api/conversations/${CONV_A}`, apiError(503, "service_unavailable", { retryable: true }));
  env.ctl.askDeleteConversation(CONV_A);
  const result = await env.ctl.deleteConversation();
  assert.equal(result.ok, false);

  release.resolve();
  await listing;
  await env.settle();

  const view = env.view();
  assert.deepEqual(view.list.items.map((item) => item.label), ["Новое имя"]);
  assert.equal(view.list.status, "ready");
  assert.equal(view.currentId, CONV_A);
  assert.equal(view.list.deleting.phase, "error", "the failed deletion is still there with its retry");
});

// ============================================================================================
// Shopping lists
// ============================================================================================

const SHOP_URL = "/api/shopping-lists";
const LIST_URL_1 = `/api/shopping-lists/${H.LIST_1}`;
const LIST_URL_2 = `/api/shopping-lists/${H.LIST_2}`;
const RECIPE_URL = `/api/conversations/${CONV_A}/messages/2/recipes/0`;
const ITEM_4 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc4";
const BORSH_INGREDIENTS = [
  { name: "свёкла", amount: "2 шт." },
  { name: "капуста", amount: "300 г" },
  { name: "говядина", amount: "500 г" },
];
const BORSH_ITEMS = [
  H.shoppingItem(H.ITEM_1, 0, "свёкла", "2 шт.", "at_home"),
  H.shoppingItem(H.ITEM_2, 1, "капуста", "300 г", "unchecked"),
  H.shoppingItem(H.ITEM_3, 2, "говядина", "500 г", "unchecked"),
];

function borshList(status = "active", items = BORSH_ITEMS) {
  return H.shoppingDetail(
    H.LIST_1,
    "Борщ",
    status,
    items.map((item) => ({ ...item })),
    { created: true }
  );
}

const shoppingListBody = (detail) => H.shoppingListsBody([H.shoppingSummary(detail)]);

async function withShopping() {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]),
  });
  env.net.route("GET", RECIPE_URL, H.recipeBody(2, 0, "Борщ", "4 порции", BORSH_INGREDIENTS, ["Сварите бульон."]));
  await env.boot();
  return env;
}

async function openedShoppingList(extra = {}) {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  env.net.route("GET", LIST_URL_1, json(200, borshList()));
  await env.ctl.showSection("shopping");
  await env.ctl.openShoppingList(H.LIST_1);
  await env.settle();
  return env;
}

scenario("a_recipe_opens_a_draft_with_every_ingredient_and_nothing_marked_at_home", async () => {
  const env = await withShopping();

  const result = await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  await env.settle();

  assert.deepEqual(result, { ok: true });
  const view = env.view();
  assert.equal(view.section, "shopping");
  assert.equal(view.shopping.title, Logic.TEXT.shoppingDraftTitle);
  const draft = view.shopping.draft;
  assert.deepEqual(
    [draft.title, draft.servings, draft.total, draft.remaining, draft.canSave, draft.editable],
    ["Борщ", "4 порции", 3, 3, true, true]
  );
  assert.deepEqual(
    draft.ingredients.map((ingredient) => [ingredient.name, ingredient.amount, ingredient.atHome]),
    [
      ["свёкла", "2 шт.", false],
      ["капуста", "300 г", false],
      ["говядина", "500 г", false],
    ]
  );
  assert.deepEqual(env.net.requests("GET", RECIPE_URL).map((request) => request.url), [RECIPE_URL]);
  assert.equal(env.net.requests("POST", SHOP_URL).length, 0, "only the person's save creates a list");
  assert.equal(env.ctl.toggleShoppingDraftAtHome(1, true), true);
  assert.equal(env.view().shopping.draft.remaining, 2);
});

scenario("the_draft_saves_only_the_marked_ingredients_as_at_home_and_opens_the_list", async () => {
  const env = await withShopping();
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  env.net.route("POST", SHOP_URL, json(201, borshList()));

  env.ctl.toggleShoppingDraftAtHome(0, true);
  env.ctl.toggleShoppingDraftAtHome(2, true);
  const result = await env.ctl.saveShoppingDraft();
  await env.settle();

  assert.equal(result.ok, true);
  const [request] = env.net.requests("POST", SHOP_URL);
  assert.deepEqual(request.body, {
    source: "message",
    conversation_id: CONV_A,
    message_id: 2,
    recipe_index: 0,
    item_states: [
      { position: 0, state: "at_home" },
      { position: 2, state: "at_home" },
    ],
  });
  assert.match(request.headers["idempotency-key"], /^web-[0-9a-f]+$/);
  const view = env.view();
  assert.equal(view.shopping.draft, null);
  assert.equal(view.section, "shopping");
  assert.deepEqual([view.shopping.current.id, view.shopping.current.title], [H.LIST_1, "Борщ"]);
  assert.deepEqual(view.shopping.current.counts, { total: 3, unchecked: 2, at_home: 1, purchased: 0 });
  assert.equal(view.shopping.current.progress, "Куплено 0 из 3");
  assert.deepEqual(view.shopping.lists.map((item) => item.id), [H.LIST_1]);
  assert.ok(env.announced.includes(Logic.TEXT.shoppingSaved));
});

scenario("an_unclear_save_is_retried_with_the_same_key_and_the_same_frozen_payload", async () => {
  const env = await withShopping();
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  env.net.route("POST", SHOP_URL, fail(), json(200, { ...borshList(), created: false }));

  env.ctl.toggleShoppingDraftAtHome(1, true);
  const first = await env.ctl.saveShoppingDraft();
  await env.settle();

  assert.deepEqual([first.ok, first.reason], [false, "uncertain"]);
  let draft = env.view().shopping.draft;
  assert.equal(draft.phase, "uncertain");
  assert.equal(draft.canRetry, true);
  assert.equal(draft.editable, false, "the selection cannot change while the key must stay identical");
  assert.equal(env.ctl.toggleShoppingDraftAtHome(0, true), false);
  assert.equal(draft.error.text, Logic.TEXT.shoppingUncertain);
  const [firstRequest] = env.net.requests("POST", SHOP_URL);

  const second = await env.ctl.retryShoppingDraft();
  await env.settle();

  assert.equal(second.ok, true);
  const requests = env.net.requests("POST", SHOP_URL);
  assert.equal(requests.length, 2);
  assert.equal(requests[1].headers["idempotency-key"], firstRequest.headers["idempotency-key"]);
  assert.deepEqual(requests[1].body, firstRequest.body);
  assert.equal(env.view().shopping.draft, null);
  assert.equal(env.view().shopping.current.status, "ready");
});

scenario("a_second_save_while_one_is_pending_sends_nothing_and_cannot_be_cancelled", async () => {
  const env = await withShopping();
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  const gate = deferred();
  env.net.route("POST", SHOP_URL, async () => {
    await gate.promise;
    return json(201, borshList());
  });

  const first = env.ctl.saveShoppingDraft();
  await flush();
  assert.equal(env.view().shopping.draft.phase, "saving");
  assert.deepEqual(await env.ctl.saveShoppingDraft(), { ok: false, reason: "not_ready" });
  assert.deepEqual(env.ctl.cancelShoppingDraft(), { ok: false, reason: "busy" });
  gate.resolve();
  await first;
  await env.settle();

  assert.equal(env.net.requests("POST", SHOP_URL).length, 1);
});

scenario("a_reused_key_is_shown_and_only_an_explicit_click_makes_a_new_key", async () => {
  const env = await withShopping();
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  env.net.route("POST", SHOP_URL, apiError(409, "shopping_list_key_reused"), json(201, borshList()));

  env.ctl.toggleShoppingDraftAtHome(1, true);
  const refused = await env.ctl.saveShoppingDraft();
  await env.settle();

  assert.deepEqual([refused.ok, refused.reason], [false, "reused"]);
  let draft = env.view().shopping.draft;
  assert.equal(draft.phase, "reused");
  assert.equal(draft.canSaveNew, true);
  assert.equal(draft.error.text, Logic.TEXT.shoppingKeyReused);
  assert.equal(draft.editable, true, "the draft can be reviewed before saving again");
  assert.deepEqual(await env.ctl.saveShoppingDraft(), { ok: false, reason: "not_ready" });
  assert.deepEqual(await env.ctl.retryShoppingDraft(), { ok: false, reason: "not_ready" });

  const [first] = env.net.requests("POST", SHOP_URL);
  env.ctl.toggleShoppingDraftAtHome(2, true); // the review changed the draft before the explicit new save
  const saved = await env.ctl.saveShoppingDraftWithNewKey();
  await env.settle();

  assert.equal(saved.ok, true);
  const requests = env.net.requests("POST", SHOP_URL);
  assert.equal(requests.length, 2);
  assert.notEqual(requests[1].headers["idempotency-key"], first.headers["idempotency-key"]);
  assert.deepEqual(requests[1].body.item_states, [
    { position: 1, state: "at_home" },
    { position: 2, state: "at_home" },
  ]);
});

scenario("a_deleted_list_key_is_not_recreated_by_anything_the_page_can_do", async () => {
  const env = await withShopping();
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  env.net.route("POST", SHOP_URL, apiError(409, "shopping_list_deleted"));

  const refused = await env.ctl.saveShoppingDraft();
  await env.settle();

  assert.deepEqual([refused.ok, refused.reason], [false, "deleted"]);
  const draft = env.view().shopping.draft;
  assert.equal(draft.phase, "deleted");
  assert.equal(draft.terminal, true);
  assert.equal(draft.canSaveNew, false);
  assert.equal(draft.canRetry, false);
  assert.equal(draft.error.text, Logic.TEXT.shoppingDeletedElsewhere);
  assert.deepEqual(await env.ctl.saveShoppingDraftWithNewKey(), { ok: false, reason: "not_ready" });
  assert.deepEqual(await env.ctl.retryShoppingDraft(), { ok: false, reason: "not_ready" });
  assert.equal(env.net.requests("POST", SHOP_URL).length, 1);
});

scenario("a_full_account_shows_the_servers_words_and_an_explicit_new_save_is_possible", async () => {
  const env = await withShopping();
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  env.net.route("POST", SHOP_URL, apiError(409, "shopping_lists_full"), json(201, borshList()));

  const refused = await env.ctl.saveShoppingDraft();
  await env.settle();

  assert.equal(refused.reason, "rejected");
  let draft = env.view().shopping.draft;
  assert.equal(draft.phase, "rejected");
  assert.equal(draft.error.text, Logic.CODE_MESSAGES.shopping_lists_full);
  assert.equal(draft.canSaveNew, true);
  assert.equal(draft.saveNewLabel, Logic.TEXT.shoppingSaveAgain);

  assert.equal((await env.ctl.saveShoppingDraftWithNewKey()).ok, true);
  assert.equal(env.view().shopping.draft, null);
});

scenario("cancelling_a_draft_returns_to_whence_it_was_opened_without_a_list", async () => {
  const env = await withShopping();
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  assert.equal(env.view().section, "shopping");

  const cancelled = env.ctl.cancelShoppingDraft();
  assert.deepEqual(cancelled, { ok: true, origin: "chats" });
  assert.equal(env.view().section, "chats");
  assert.equal(env.view().shopping.draft, null);
  assert.equal(env.net.requests("POST", SHOP_URL).length, 0);
});

scenario("a_structured_favorite_makes_a_draft_from_the_card_and_its_version", async () => {
  const structured = H.structuredFavorite(H.FAV_1, 2, "Борщ", 0, 0, { version: 3 });
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]),
  });
  env.net.route("GET", "/api/favorites", H.favoritesBody([structured]));
  env.net.route(
    "GET",
    `/api/favorites/${H.FAV_1}`,
    H.favoriteDetail(structured, "Борщ", {
      servings: "4 порции",
      ingredients: BORSH_INGREDIENTS,
      steps: ["Сварите бульон."],
    })
  );
  env.net.route("POST", SHOP_URL, json(201, borshList()));
  await env.boot();
  await env.ctl.openFavorite(H.FAV_1);

  const opened = env.ctl.beginShoppingDraftFromFavorite();
  assert.equal(opened.ok, true);
  assert.equal(env.view().section, "shopping");
  assert.deepEqual(
    env.view().shopping.draft.ingredients.map((ingredient) => ingredient.name),
    ["свёкла", "капуста", "говядина"]
  );
  env.ctl.toggleShoppingDraftAtHome(1, true);
  const saved = await env.ctl.saveShoppingDraft();
  await env.settle();

  assert.equal(saved.ok, true);
  assert.deepEqual(env.net.requests("POST", SHOP_URL)[0].body, {
    source: "favorite",
    favorite_id: H.FAV_1,
    expected_version: 3,
    item_states: [{ position: 1, state: "at_home" }],
  });
});

scenario("a_legacy_text_favorite_offers_no_shopping_draft", async () => {
  const env = await withFavorites([F1]);
  await env.ctl.openFavorite(H.FAV_1);

  assert.deepEqual(env.ctl.beginShoppingDraftFromFavorite(), { ok: false, reason: "not_ready" });
  assert.equal(env.view().section, "favorites");
  assert.equal(env.net.requests("GET", RECIPE_URL).length, 0);
  assert.equal(env.net.requests("POST", SHOP_URL).length, 0);
});

scenario("the_shopping_section_lists_active_lists_before_completed_ones_with_progress_and_status", async () => {
  const active = borshList();
  active.items[0].state = "purchased";
  const completed = H.shoppingDetail(H.LIST_2, "Плов", "completed", [
    H.shoppingItem(ITEM_4, 0, "рис", "200 г", "purchased"),
  ]);
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, H.shoppingListsBody([H.shoppingSummary(completed), H.shoppingSummary(active)]));

  env.ctl.showSection("shopping");
  await env.settle();

  const view = env.view();
  assert.equal(view.section, "shopping");
  assert.deepEqual(view.shopping.lists.map((item) => item.id), [H.LIST_1, H.LIST_2]);
  assert.equal(view.shopping.lists[0].meta.startsWith("Куплено 1 из 3 · В работе"), true, view.shopping.lists[0].meta);
  assert.equal(view.shopping.lists[1].meta.startsWith("Куплено 1 из 1 · Завершен"), true, view.shopping.lists[1].meta);
  assert.equal(view.shopping.empty, false);
});

scenario("returning_to_the_shopping_section_reads_the_lists_again", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, H.shoppingListsBody([]));

  env.ctl.showSection("shopping");
  await env.settle();
  assert.equal(env.view().shopping.empty, true);
  env.ctl.showSection("chats");
  env.ctl.showSection("shopping");
  await env.settle();

  assert.equal(env.net.requests("GET", SHOP_URL).length, 2);
});

scenario("a_saved_list_shows_every_item_grouped_by_state_and_counts_them", async () => {
  const env = await openedShoppingList();

  const current = env.view().shopping.current;
  assert.deepEqual(
    [current.id, current.title, current.status, current.readOnly],
    [H.LIST_1, "Борщ", "ready", false]
  );
  assert.deepEqual(current.counts, { total: 3, unchecked: 2, at_home: 1, purchased: 0 });
  assert.deepEqual(current.groups.map((group) => [group.title, group.count]), [["Проверить / купить", 2], ["Есть дома", 1]]);
  assert.deepEqual(current.groups[0].items.map((item) => item.name), ["капуста", "говядина"]);
  assert.equal(current.progress, "Куплено 0 из 3");
  assert.equal(env.view().shopping.lists[0].current, true);
});

scenario("a_state_change_is_applied_at_once_and_taken_back_when_it_fails", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  env.net.route("GET", LIST_URL_1, json(200, borshList()));
  await env.ctl.openShoppingList(H.LIST_1);
  const gate = deferred();
  env.net.route("PATCH", `${LIST_URL_1}/items/${H.ITEM_2}`, async () => {
    await gate.promise;
    return fail();
  });

  const itemById = () =>
    env.view().shopping.current.groups.reduce((all, group) => all.concat(group.items), []).filter((item) => item.id === H.ITEM_2)[0];
  const changing = env.ctl.setShoppingItemState(H.ITEM_2, "purchased");
  await flush();
  let item = itemById();
  assert.deepEqual([item.state, item.pending], ["purchased", "purchased"]);
  assert.equal(env.view().shopping.current.counts.purchased, 1);
  gate.resolve();
  const result = await changing;
  await env.settle();

  assert.equal(result.ok, false);
  assert.equal(env.view().shopping.current.counts.purchased, 0);
  item = itemById();
  assert.deepEqual([item.state, item.pending], ["unchecked", null]);
  assert.equal(env.view().shopping.current.actionError.text, Logic.TEXT.network);
});

scenario("overlapping_item_state_changes_are_serialized_and_the_first_settles_after_a_refusal", async () => {
  const env = await openedShoppingList();
  const gate = deferred();
  env.net.route(
    "PATCH",
    `${LIST_URL_1}/items/${H.ITEM_2}`,
    async () => {
      await gate.promise;
      return apiError(400, "invalid_request");
    },
    json(200, H.shoppingItem(H.ITEM_2, 1, "капуста", "300 г", "purchased"))
  );
  env.net.route("PATCH", `${LIST_URL_1}/items/${H.ITEM_3}`, json(200, H.shoppingItem(H.ITEM_3, 2, "говядина", "500 г", "purchased")));
  const itemById = (id) =>
    env.view().shopping.current.groups.reduce((all, group) => all.concat(group.items), []).filter((item) => item.id === id)[0];

  const first = env.ctl.setShoppingItemState(H.ITEM_2, "purchased");
  await flush();
  assert.deepEqual([itemById(H.ITEM_2).state, itemById(H.ITEM_2).pending], ["purchased", "purchased"]);
  assert.equal(env.view().shopping.current.busy, true);

  // The second change is refused while the first is out: no request, no optimistic state.
  assert.deepEqual(await env.ctl.setShoppingItemState(H.ITEM_3, "purchased"), { ok: false, reason: "busy" });
  assert.equal(env.net.requests("PATCH", `${LIST_URL_1}/items/${H.ITEM_3}`).length, 0);
  assert.deepEqual([itemById(H.ITEM_3).state, itemById(H.ITEM_3).pending], ["unchecked", null]);

  gate.resolve();
  const result = await first;
  await env.settle();
  assert.deepEqual(result, { ok: false, reason: "rejected" });
  assert.deepEqual([itemById(H.ITEM_2).state, itemById(H.ITEM_2).pending], ["unchecked", null], "the pending state is cleared");
  assert.equal(env.view().shopping.current.busy, false);
  assert.equal(env.view().shopping.current.actionError.text, Logic.CODE_MESSAGES.invalid_request);

  // And the same item can be changed again now that the first write has settled.
  const second = await env.ctl.setShoppingItemState(H.ITEM_2, "purchased");
  await env.settle();
  assert.equal(second.ok, true);
  assert.deepEqual([itemById(H.ITEM_2).state, itemById(H.ITEM_2).pending], ["purchased", null]);
  assert.equal(env.view().shopping.current.counts.purchased, 1);
});

scenario("an_edit_held_open_cannot_be_overlapped_and_reaches_a_terminal_state_either_way", async () => {
  const env = await openedShoppingList();
  const gate = deferred();
  env.net.route(
    "PATCH",
    `${LIST_URL_1}/items/${H.ITEM_2}`,
    async () => {
      await gate.promise;
      return apiError(400, "invalid_request");
    },
    json(200, H.shoppingItem(H.ITEM_2, 1, "капуста белая", "300 г", "unchecked"))
  );
  env.net.route("PATCH", `${LIST_URL_1}/items/${H.ITEM_3}`, json(200, H.shoppingItem(H.ITEM_3, 2, "говядина", "500 г", "purchased")));
  env.net.route("PATCH", LIST_URL_1, json(200, borshList("completed")));

  assert.equal(env.ctl.startShoppingItemEdit(H.ITEM_2), true);
  const saving = env.ctl.saveShoppingItemEdit({ name: "капуста белая", amount: "300 г" });
  await flush();
  assert.equal(env.view().shopping.current.editor.phase, "saving");
  assert.equal(env.view().shopping.current.busy, true);

  // While the editor is saving, another item's change is refused and cannot start.
  assert.deepEqual(await env.ctl.setShoppingItemState(H.ITEM_3, "purchased"), { ok: false, reason: "busy" });
  assert.equal(env.ctl.beginShoppingItemAdd(), false);
  assert.equal(env.ctl.askDeleteShoppingItem(H.ITEM_3), false);
  assert.equal(env.net.requests("PATCH", `${LIST_URL_1}/items/${H.ITEM_3}`).length, 0);

  gate.resolve();
  const refused = await saving;
  await env.settle();
  assert.deepEqual(refused, { ok: false, reason: "rejected" });
  let editor = env.view().shopping.current.editor;
  assert.equal(editor.phase, "editing", "the editor never stays in saving");
  assert.equal(editor.error.text, Logic.CODE_MESSAGES.invalid_request);
  assert.equal(env.view().shopping.current.busy, false);

  // Cancellation, editing and completing the list all work again.
  assert.equal(env.ctl.cancelShoppingItemEdit(), true);
  assert.equal(env.view().shopping.current.editor, null);
  assert.equal(env.ctl.startShoppingItemEdit(H.ITEM_2), true);
  const saved = await env.ctl.saveShoppingItemEdit({ name: "капуста белая", amount: "300 г" });
  await env.settle();
  assert.equal(saved.ok, true);
  assert.equal(env.view().shopping.current.editor, null);
  const completed = await env.ctl.setShoppingListStatus("completed");
  await env.settle();
  assert.equal(completed.ok, true);
  assert.equal(env.view().shopping.current.readOnly, true);
});

scenario("a_valid_position_at_or_beyond_the_item_cap_is_accepted", async () => {
  const env = await withShopping();
  const atCap = H.shoppingItem(ITEM_4, 200, "соль", "1 ч. л.", "unchecked");
  const items = BORSH_ITEMS.concat([atCap]);
  const detail = H.shoppingDetail(H.LIST_1, "Борщ", "active", items.map((item) => ({ ...item })));
  env.net.route("GET", SHOP_URL, H.shoppingListsBody([H.shoppingSummary(detail)]));
  env.net.route("GET", LIST_URL_1, json(200, detail), json(200, detail));
  env.net.route(
    "PATCH",
    `${LIST_URL_1}/items/${ITEM_4}`,
    json(200, H.shoppingItem(ITEM_4, 200, "соль морская", "1 ч. л.", "unchecked")),
    json(200, H.shoppingItem(ITEM_4, 200, "соль морская", "1 ч. л.", "purchased"))
  );

  const opened = await env.ctl.openShoppingList(H.LIST_1);
  await env.settle();
  assert.deepEqual(opened, { ok: true });
  const itemById = (id) =>
    env.view().shopping.current.groups.reduce((all, group) => all.concat(group.items), []).filter((item) => item.id === id)[0];
  assert.equal(env.view().shopping.current.itemCount, 4);
  assert.equal(itemById(ITEM_4).position, 200);

  assert.equal(env.ctl.startShoppingItemEdit(ITEM_4), true);
  const edited = await env.ctl.saveShoppingItemEdit({ name: "соль морская", amount: "1 ч. л." });
  await env.settle();
  assert.equal(edited.ok, true);
  assert.equal(itemById(ITEM_4).position, 200);
  assert.equal(itemById(ITEM_4).name, "соль морская");

  const purchased = await env.ctl.setShoppingItemState(ITEM_4, "purchased");
  await env.settle();
  assert.equal(purchased.ok, true);
  assert.deepEqual([itemById(ITEM_4).position, itemById(ITEM_4).state], [200, "purchased"]);

  const reopened = await env.ctl.openShoppingList(H.LIST_1);
  await env.settle();
  assert.deepEqual(reopened, { ok: true });
  assert.equal(env.view().shopping.current.itemCount, 4, "the list opens again with the high position");
});

scenario("a_write_that_finishes_after_the_session_ended_changes_nothing", async () => {
  const env = await openedShoppingList();
  const gate = deferred();
  env.net.route("PATCH", `${LIST_URL_1}/items/${H.ITEM_2}`, async () => {
    await gate.promise;
    return json(200, H.shoppingItem(H.ITEM_2, 1, "капуста", "300 г", "purchased"));
  });

  const changing = env.ctl.setShoppingItemState(H.ITEM_2, "purchased");
  await flush();
  await env.timers.advance(86400 * 1000 + 1000);
  env.ctl.checkSession();
  assert.equal(env.view().screen, "login");

  gate.resolve();
  assert.deepEqual(await changing, { ok: false, reason: "stale" });
  await env.settle();
  assert.equal(env.view().screen, "login");
  assert.deepEqual(env.view().shopping.lists, []);
  assert.equal(env.view().shopping.current, null);
});

scenario("a_completed_list_is_read_only_and_can_be_reopened", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList("completed")));
  env.net.route("GET", LIST_URL_1, json(200, borshList("completed")));
  env.net.route(
    "PATCH",
    LIST_URL_1,
    json(200, borshList("active"))
  );
  await env.ctl.openShoppingList(H.LIST_1);

  let current = env.view().shopping.current;
  assert.equal(current.readOnly, true);
  assert.equal(current.status, "ready");
  assert.deepEqual(await env.ctl.setShoppingItemState(H.ITEM_2, "purchased"), { ok: false, reason: "not_ready" });
  assert.equal(env.net.requests("PATCH", new RegExp("/items/")).length, 0);

  const reopened = await env.ctl.setShoppingListStatus("active");
  await env.settle();
  assert.equal(reopened.ok, true);
  current = env.view().shopping.current;
  assert.equal(current.readOnly, false);
  assert.deepEqual([current.readOnly, env.view().shopping.lists[0].meta.includes("В работе")], [false, true]);
  assert.ok(env.announced.includes(Logic.TEXT.listReopened));
});

scenario("items_are_added_edited_and_removed_with_the_servers_own_bodies", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  env.net.route("GET", LIST_URL_1, json(200, borshList()));
  const added = H.shoppingItem(ITEM_4, 3, "соль", "1 ч. л.", "unchecked");
  env.net.route("POST", `${LIST_URL_1}/items`, json(201, added));
  env.net.route(
    "PATCH",
    `${LIST_URL_1}/items/${H.ITEM_2}`,
    json(200, H.shoppingItem(H.ITEM_2, 1, "капуста белая", null, "unchecked"))
  );
  env.net.route("DELETE", `${LIST_URL_1}/items/${H.ITEM_3}`, H.removedBody());
  await env.ctl.openShoppingList(H.LIST_1);

  assert.equal(env.ctl.beginShoppingItemAdd(), true);
  const addedResult = await env.ctl.saveShoppingItemAdd({ name: "  соль ", amount: " 1 ч. л. " });
  await env.settle();
  assert.equal(addedResult.ok, true);
  assert.deepEqual(env.net.requests("POST", `${LIST_URL_1}/items`)[0].body, {
    name: "соль",
    amount: "1 ч. л.",
  });
  assert.equal(env.view().shopping.current.counts.total, 4);
  assert.ok(env.announced.includes(Logic.TEXT.itemAdded));

  assert.equal(env.ctl.startShoppingItemEdit(H.ITEM_2), true);
  const edited = await env.ctl.saveShoppingItemEdit({ name: " капуста белая ", amount: " " });
  await env.settle();
  assert.equal(edited.ok, true);
  assert.deepEqual(env.net.requests("PATCH", `${LIST_URL_1}/items/${H.ITEM_2}`)[0].body, {
    name: "капуста белая",
    amount: null,
  });
  let items = env.view().shopping.current.groups.filter((group) => group.state === "unchecked")[0].items;
  assert.equal(items.some((item) => item.name === "капуста белая" && item.amount === null), true);

  assert.equal(env.ctl.askDeleteShoppingItem(H.ITEM_3), true);
  assert.equal(env.net.requests("DELETE", /.*/).length, 0, "asking deletes nothing");
  assert.equal(env.ctl.cancelDeleteShoppingItem(), true);
  assert.equal(env.ctl.askDeleteShoppingItem(H.ITEM_3), true);
  const removed = await env.ctl.deleteShoppingItem();
  await env.settle();
  assert.equal(removed.ok, true);
  assert.equal(env.net.requests("DELETE", `${LIST_URL_1}/items/${H.ITEM_3}`).length, 1);
  items = env.view().shopping.current.groups.reduce((all, group) => all.concat(group.items), []);
  assert.equal(items.some((item) => item.id === H.ITEM_3), false);
  assert.equal(env.view().shopping.current.counts.total, 3);
  assert.ok(env.announced.includes(Logic.TEXT.itemRemoved));
});

scenario("a_refused_edit_keeps_the_persons_words_and_says_why", async () => {
  const env = await openedShoppingList();
  env.net.route("PATCH", `${LIST_URL_1}/items/${H.ITEM_2}`, apiError(400, "invalid_request"));

  env.ctl.startShoppingItemEdit(H.ITEM_2);
  const result = await env.ctl.saveShoppingItemEdit({ name: "капуста белая", amount: "300 г" });
  await env.settle();

  assert.equal(result.ok, false);
  const editor = env.view().shopping.current.editor;
  assert.equal(editor.phase, "editing");
  assert.equal(editor.error.text, Logic.CODE_MESSAGES.invalid_request);
  assert.deepEqual(editor.draft, { name: "капуста белая", amount: "300 г" });
  assert.equal(env.view().shopping.current.groups.some((group) => group.items.some((item) => item.name === "капуста белая")), false);
});

scenario("an_add_whose_answer_is_lost_keeps_the_typed_values_and_is_never_resent", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  env.net.route("GET", LIST_URL_1, json(200, borshList()), json(200, borshList()));
  env.net.route("POST", `${LIST_URL_1}/items`, fail());
  await env.ctl.openShoppingList(H.LIST_1);

  env.ctl.beginShoppingItemAdd();
  const result = await env.ctl.saveShoppingItemAdd({ name: "  соль ", amount: " 1 ч. л. " });
  await env.settle();

  assert.equal(result.ok, false);
  assert.equal(env.net.requests("POST", `${LIST_URL_1}/items`).length, 1, "nothing is sent again by itself");
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 2, "the list is read again for the truth");
  const adding = env.view().shopping.current.adding;
  assert.ok(adding, "the form is still there");
  assert.equal(adding.phase, "uncertain");
  assert.deepEqual(adding.draft, { name: "  соль ", amount: " 1 ч. л. " }, "what was typed is kept");
  assert.equal(adding.error.text, Logic.TEXT.addItemUncertain);
  assert.equal(env.view().shopping.current.actionError, null);

  // The person may send it again, explicitly: the second click is the one that POSTs.
  const added = H.shoppingItem(ITEM_4, 3, "соль", "1 ч. л.", "unchecked");
  env.net.route("POST", `${LIST_URL_1}/items`, json(201, added));
  const again = await env.ctl.saveShoppingItemAdd({ name: "соль", amount: "1 ч. л." });
  await env.settle();
  assert.equal(again.ok, true);
  assert.equal(env.net.requests("POST", `${LIST_URL_1}/items`).length, 2);
  assert.equal(env.view().shopping.current.adding, null);
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 2, "a certain answer needs no second read");
});

scenario("an_add_whose_lost_answer_turned_out_to_have_been_saved_closes_with_the_servers_item", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  const added = H.shoppingItem(ITEM_4, 3, "соль", null, "unchecked");
  env.net.route(
    "GET",
    LIST_URL_1,
    json(200, borshList()),
    json(200, borshList("active", BORSH_ITEMS.concat([added])))
  );
  env.net.route("POST", `${LIST_URL_1}/items`, fail());
  await env.ctl.openShoppingList(H.LIST_1);

  env.ctl.beginShoppingItemAdd();
  const result = await env.ctl.saveShoppingItemAdd({ name: "соль", amount: "" });
  await env.settle();

  assert.equal(result.ok, true);
  assert.equal(result.id, ITEM_4);
  assert.equal(env.net.requests("POST", `${LIST_URL_1}/items`).length, 1, "the first request is never repeated");
  assert.equal(env.view().shopping.current.adding, null, "the item the server really has closes the form");
  assert.ok(env.announced.includes(Logic.TEXT.itemAdded));
  const items = env.view().shopping.current.groups.reduce((all, group) => all.concat(group.items), []);
  assert.ok(items.some((item) => item.id === ITEM_4 && item.name === "соль"));
});

scenario("an_ambiguous_add_on_a_list_completed_meanwhile_keeps_its_form_and_can_be_reopened", async () => {
  // The review's reproduction: the POST's answer is lost, the true list has meanwhile been
  // completed elsewhere and does not name the item. That proves nothing about the POST, so the
  // form keeps the words; the completed list must offer its warning and cancellation, and
  // «Вернуть в работу» must not be refused by that form.
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  env.net.route("GET", LIST_URL_1, json(200, borshList()), json(200, borshList("completed")));
  env.net.route("POST", `${LIST_URL_1}/items`, fail());
  await env.ctl.openShoppingList(H.LIST_1);

  env.ctl.beginShoppingItemAdd();
  const result = await env.ctl.saveShoppingItemAdd({ name: "соль", amount: "1 ч. л." });
  await env.settle();

  assert.deepEqual(result, { ok: false, reason: "uncertain" });
  let current = env.view().shopping.current;
  assert.equal(current.readOnly, true, "the list is completed now");
  assert.equal(current.adding.phase, "uncertain");
  assert.deepEqual(current.adding.draft, { name: "соль", amount: "1 ч. л." }, "what was typed is kept");
  assert.equal(current.adding.error.text, Logic.TEXT.addItemUncertain);
  assert.equal(current.busy, false, "no write is in flight");

  // A completed list sends no new item, even from the retained form.
  assert.deepEqual(await env.ctl.saveShoppingItemAdd({ name: "соль", amount: "1 ч. л." }), {
    ok: false,
    reason: "not_ready",
  });
  assert.equal(env.net.requests("POST", `${LIST_URL_1}/items`).length, 1, "nothing is sent again by itself");

  // The unresolved form does not hold the list hostage: it can be returned to work.
  env.net.route("PATCH", LIST_URL_1, json(200, borshList("active")));
  const reopened = await env.ctl.setShoppingListStatus("active");
  await env.settle();
  assert.equal(reopened.ok, true);
  current = env.view().shopping.current;
  assert.equal(current.readOnly, false);
  assert.equal(current.adding.phase, "uncertain", "reopening keeps the unresolved form");
  assert.deepEqual(current.adding.draft, { name: "соль", amount: "1 ч. л." });
  assert.equal(
    env.net.requests("POST", `${LIST_URL_1}/items`).length,
    1,
    "reopening never retries the original POST"
  );

  // Now the person can send the retained values explicitly.
  const added = H.shoppingItem(ITEM_4, 3, "соль", "1 ч. л.", "unchecked");
  env.net.route("POST", `${LIST_URL_1}/items`, json(201, added));
  const again = await env.ctl.saveShoppingItemAdd({ name: "соль", amount: "1 ч. л." });
  await env.settle();
  assert.equal(again.ok, true);
  assert.equal(again.id, ITEM_4);
  assert.equal(env.net.requests("POST", `${LIST_URL_1}/items`).length, 2, "the second click is the one that sends");
  assert.equal(env.view().shopping.current.adding, null);
});

scenario("an_unresolved_add_can_be_cancelled_from_a_completed_list", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  env.net.route("GET", LIST_URL_1, json(200, borshList()), json(200, borshList("completed")));
  env.net.route("POST", `${LIST_URL_1}/items`, fail());
  await env.ctl.openShoppingList(H.LIST_1);

  env.ctl.beginShoppingItemAdd();
  await env.ctl.saveShoppingItemAdd({ name: "соль", amount: "1 ч. л." });
  await env.settle();
  assert.equal(env.view().shopping.current.readOnly, true);
  assert.equal(env.view().shopping.current.adding.phase, "uncertain");

  assert.equal(env.ctl.cancelShoppingItemAdd(), true);
  const current = env.view().shopping.current;
  assert.equal(current.adding, null);
  assert.equal(current.readOnly, true, "cancelling the form changes nothing of the list");
  assert.equal(current.itemCount, 3, "the list data is untouched");
  assert.equal(env.net.requests("POST", `${LIST_URL_1}/items`).length, 1);

  // Nothing hidden blocks the list afterwards.
  env.net.route("PATCH", LIST_URL_1, json(200, borshList("active")));
  const reopened = await env.ctl.setShoppingListStatus("active");
  await env.settle();
  assert.equal(reopened.ok, true);
  assert.equal(env.view().shopping.current.readOnly, false);
});

scenario("a_session_that_ends_while_a_completed_list_is_being_reopened_leaves_no_hidden_state", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  env.net.route("GET", LIST_URL_1, json(200, borshList()), json(200, borshList("completed")));
  env.net.route("POST", `${LIST_URL_1}/items`, fail());
  await env.ctl.openShoppingList(H.LIST_1);
  env.ctl.beginShoppingItemAdd();
  await env.ctl.saveShoppingItemAdd({ name: "соль", amount: "1 ч. л." });
  await env.settle();
  assert.equal(env.view().shopping.current.readOnly, true);

  const reopenGate = deferred();
  env.net.route("PATCH", LIST_URL_1, async () => {
    await reopenGate.promise;
    return json(200, borshList("active"));
  });
  const reopening = env.ctl.setShoppingListStatus("active");
  await flush();
  assert.equal(env.view().shopping.current.busy, true);

  await env.timers.advance(86400 * 1000 + 1000);
  env.ctl.checkSession();
  assert.equal(env.view().screen, "login");

  reopenGate.resolve();
  assert.deepEqual(await reopening, { ok: false, reason: "stale" });
  await env.settle();
  assert.equal(env.view().screen, "login");
  assert.equal(env.view().shopping.current, null);
  assert.deepEqual(env.view().shopping.lists, []);
  assert.deepEqual(await env.ctl.setShoppingListStatus("active"), { ok: false, reason: "not_ready" });
});

scenario("deleting_a_list_asks_first_then_removes_it_and_leaves_no_draft", async () => {
  const env = await openedShoppingList();
  env.net.route("DELETE", LIST_URL_1, H.removedBody());

  assert.equal(env.ctl.askDeleteShoppingList(), true);
  assert.equal(env.view().shopping.current.deleting.phase, "asking");
  assert.equal(env.net.requests("DELETE", /.*/).length, 0);
  assert.equal(env.ctl.cancelDeleteShoppingList(), true);
  assert.equal(env.view().shopping.current.deleting, null);
  assert.equal(env.ctl.askDeleteShoppingList(), true);
  const result = await env.ctl.deleteShoppingList();
  await env.settle();

  assert.equal(result.ok, true);
  assert.equal(env.view().shopping.current, null);
  assert.deepEqual(env.view().shopping.lists, []);
  assert.equal(env.view().shopping.draft, null);
  assert.ok(env.announced.includes(Logic.TEXT.listDeleted));
  assert.equal(env.net.requests("POST", SHOP_URL).length, 0, "the old save request is never used");
});

scenario("a_confirmed_deletion_is_not_suppressed_by_a_re_entry_or_an_older_read", async () => {
  const env = await openedShoppingList();
  const readGate = deferred();
  env.net.route("GET", LIST_URL_1, async () => {
    await readGate.promise;
    return json(200, borshList());
  });
  const deleteGate = deferred();
  env.net.route("DELETE", LIST_URL_1, async () => {
    await deleteGate.promise;
    return H.removedBody();
  });

  // A read of the list that began before the deletion, still on its way.
  env.ctl.recheck();
  await flush();
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 2);

  assert.equal(env.ctl.askDeleteShoppingList(), true);
  const deleting = env.ctl.deleteShoppingList();
  await flush();

  // Re-entering the list while the deletion is unsettled starts no read and changes nothing.
  assert.deepEqual(await env.ctl.openShoppingList(H.LIST_1), { ok: false, reason: "busy" });
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 2);
  assert.equal(env.view().shopping.current.id, H.LIST_1);

  deleteGate.resolve();
  assert.deepEqual(await deleting, { ok: true });
  assert.equal(env.view().shopping.current, null);
  assert.deepEqual(env.view().shopping.lists, []);
  assert.ok(env.announced.includes(Logic.TEXT.listDeleted));

  // The older read lands after the server's confirmation: it cannot put the list back.
  readGate.resolve();
  await env.settle();
  assert.equal(env.view().shopping.current, null);
  assert.deepEqual(env.view().shopping.lists, []);
});

scenario("deleting_one_list_never_makes_a_read_of_another_list_stale", async () => {
  // The review's reproduction: DELETE A is out, the person goes to B and B's read is out,
  // then A's deletion is confirmed before B's answer. B's answer is still B's, and the pane
  // must not stay on "Загружаем список…" because of a deletion of another list.
  const env = await openedShoppingList();
  const plov = H.shoppingDetail(H.LIST_2, "Плов", "active", [
    H.shoppingItem(ITEM_4, 0, "рис", "200 г", "unchecked"),
  ]);
  const deleteGate = deferred();
  const readGate = deferred();
  env.net.route("DELETE", LIST_URL_1, async () => {
    await deleteGate.promise;
    return H.removedBody();
  });
  env.net.route("GET", LIST_URL_2, async () => {
    await readGate.promise;
    return json(200, plov);
  });

  assert.equal(env.ctl.askDeleteShoppingList(), true);
  const deleting = env.ctl.deleteShoppingList();
  await flush();
  const opening = env.ctl.openShoppingList(H.LIST_2);
  await flush();
  assert.equal(env.view().shopping.current.id, H.LIST_2);
  assert.equal(env.view().shopping.current.status, "loading");

  deleteGate.resolve();
  assert.deepEqual(await deleting, { ok: true });
  await flush();
  assert.equal(env.view().shopping.current.id, H.LIST_2);
  assert.equal(env.view().shopping.current.status, "loading", "the deletion of A leaves B's own read alone");

  readGate.resolve();
  assert.deepEqual(await opening, { ok: true });
  await env.settle();
  assert.equal(env.view().shopping.current.status, "ready");
  assert.equal(env.view().shopping.current.id, H.LIST_2);
  assert.equal(env.view().shopping.current.itemCount, 1, "B's answer is applied");
  assert.deepEqual(env.view().shopping.lists.map((entry) => entry.id), [H.LIST_2], "A is gone, B is there");
  assert.equal(env.net.requests("GET", LIST_URL_2).length, 1);

  // The person can go on working with B after the odd interleaving.
  assert.equal(env.ctl.beginShoppingItemAdd(), true);
  assert.equal(env.view().shopping.current.adding.phase, "editing");
  assert.equal(env.view().shopping.current.busy, false);
});

scenario("a_late_answer_to_a_read_of_a_deleted_list_cannot_bring_it_back", async () => {
  const env = await openedShoppingList();
  const readGate = deferred();
  const deleteGate = deferred();
  env.net.route("GET", LIST_URL_1, async () => {
    await readGate.promise;
    return json(200, borshList());
  });
  env.net.route("DELETE", LIST_URL_1, async () => {
    await deleteGate.promise;
    return H.removedBody();
  });

  // A read of A that began before the deletion, still on its way.
  env.ctl.recheck();
  await flush();
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 2);

  assert.equal(env.ctl.askDeleteShoppingList(), true);
  const deleting = env.ctl.deleteShoppingList();
  await flush();
  deleteGate.resolve();
  assert.deepEqual(await deleting, { ok: true });
  assert.equal(env.view().shopping.current, null);
  assert.deepEqual(env.view().shopping.lists, []);

  // The older read lands after the server's confirmation: it cannot put the list back.
  readGate.resolve();
  await env.settle();
  assert.equal(env.view().shopping.current, null);
  assert.deepEqual(env.view().shopping.lists, []);
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 2, "late answers start no second read");
});

scenario("a_failed_deletion_does_not_disturb_another_list_being_read", async () => {
  const env = await openedShoppingList();
  const plov = H.shoppingDetail(H.LIST_2, "Плов", "active", [
    H.shoppingItem(ITEM_4, 0, "рис", "200 г", "unchecked"),
  ]);
  const deleteGate = deferred();
  const readGate = deferred();
  env.net.route("DELETE", LIST_URL_1, async () => {
    await deleteGate.promise;
    return fail();
  });
  env.net.route("GET", LIST_URL_2, async () => {
    await readGate.promise;
    return json(200, plov);
  });

  assert.equal(env.ctl.askDeleteShoppingList(), true);
  const deleting = env.ctl.deleteShoppingList();
  await flush();
  const opening = env.ctl.openShoppingList(H.LIST_2);
  await flush();

  deleteGate.resolve();
  assert.deepEqual(await deleting, { ok: false, reason: "uncertain" });
  await flush();
  assert.equal(env.view().shopping.current.id, H.LIST_2);
  assert.equal(env.view().shopping.current.status, "loading", "the failure of A's deletion leaves B's read alone");
  assert.ok(
    env.view().shopping.lists.some((entry) => entry.id === H.LIST_1),
    "a failed deletion removes nothing locally"
  );

  readGate.resolve();
  assert.deepEqual(await opening, { ok: true });
  await env.settle();
  assert.equal(env.view().shopping.current.status, "ready");
  assert.equal(env.view().shopping.current.id, H.LIST_2);
  assert.deepEqual(
    env.view().shopping.lists.map((entry) => entry.id).sort(),
    [H.LIST_1, H.LIST_2].sort(),
    "A stays until a deletion is confirmed, and B is there"
  );
});

scenario("a_session_that_ends_while_a_deletion_and_another_read_are_out_leaves_nothing_behind", async () => {
  const env = await openedShoppingList();
  const plov = H.shoppingDetail(H.LIST_2, "Плов", "active", [
    H.shoppingItem(ITEM_4, 0, "рис", "200 г", "unchecked"),
  ]);
  const deleteGate = deferred();
  const readGate = deferred();
  env.net.route("DELETE", LIST_URL_1, async () => {
    await deleteGate.promise;
    return H.removedBody();
  });
  env.net.route("GET", LIST_URL_2, async () => {
    await readGate.promise;
    return json(200, plov);
  });

  assert.equal(env.ctl.askDeleteShoppingList(), true);
  const deleting = env.ctl.deleteShoppingList();
  await flush();
  const opening = env.ctl.openShoppingList(H.LIST_2);
  await flush();

  await env.timers.advance(86400 * 1000 + 1000);
  env.ctl.checkSession();
  assert.equal(env.view().screen, "login");

  deleteGate.resolve();
  readGate.resolve();
  assert.deepEqual(await deleting, { ok: false, reason: "stale" });
  assert.deepEqual(await opening, { ok: false, reason: "stale" });
  await env.settle();
  assert.equal(env.view().screen, "login");
  assert.deepEqual(env.view().shopping.lists, []);
  assert.equal(env.view().shopping.current, null);
});

scenario("a_deletion_that_fails_keeps_the_list_and_is_never_sent_again_by_itself", async () => {
  const env = await openedShoppingList();
  env.net.route("DELETE", LIST_URL_1, fail());
  env.net.route("GET", LIST_URL_1, json(200, borshList()), json(200, borshList()));

  assert.equal(env.ctl.askDeleteShoppingList(), true);
  const result = await env.ctl.deleteShoppingList();
  await env.settle();

  assert.deepEqual(result, { ok: false, reason: "uncertain" });
  assert.equal(env.net.requests("DELETE", LIST_URL_1).length, 1, "nothing is sent again by itself");
  assert.equal(env.view().shopping.current.id, H.LIST_1, "a failed deletion removes nothing locally");
  assert.deepEqual(env.view().shopping.lists.map((item) => item.id), [H.LIST_1]);
  assert.equal(env.view().shopping.current.deleting.phase, "error");
});

scenario("a_list_removed_elsewhere_leaves_the_ui_with_an_explanation", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, H.shoppingListsBody([]));
  env.net.route("GET", LIST_URL_1, apiError(404, "shopping_list_not_found"));

  const result = await env.ctl.openShoppingList(H.LIST_1);
  await env.settle();

  assert.deepEqual(result, { ok: false, reason: "gone" });
  assert.equal(env.view().shopping.current, null);
  assert.equal(env.view().shopping.notice.text, Logic.TEXT.shoppingListGone);
});

scenario("a_late_answer_about_a_list_that_is_no_longer_open_changes_nothing", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, H.shoppingListsBody([H.shoppingSummary(borshList()), H.shoppingSummary(H.shoppingDetail(H.LIST_2, "Плов", "active", []))]));
  const slow = deferred();
  env.net.route("GET", LIST_URL_1, async () => {
    await slow.promise;
    return json(200, borshList());
  });
  env.net.route("GET", LIST_URL_2, json(200, H.shoppingDetail(H.LIST_2, "Плов", "active", [])));

  const first = env.ctl.openShoppingList(H.LIST_1);
  await flush();
  await env.ctl.openShoppingList(H.LIST_2);
  slow.resolve();
  await first;
  await env.settle();

  assert.equal(env.view().shopping.current.id, H.LIST_2);
  assert.equal(env.view().shopping.current.title, "Плов");
});

scenario("the_session_ending_clears_every_shopping_list_and_draft", async () => {
  const env = await openedShoppingList();
  env.net.route("PATCH", `${LIST_URL_1}/items/${H.ITEM_2}`, notAuthenticated());

  await env.ctl.setShoppingItemState(H.ITEM_2, "purchased");
  await env.settle();

  const view = env.view();
  assert.equal(view.screen, "login");
  assert.equal(view.section, "chats");
  assert.deepEqual(view.shopping.lists, []);
  assert.equal(view.shopping.current, null);
  assert.equal(view.shopping.draft, null);
});

scenario("another_person_signing_in_gets_no_shopping_list_of_the_last_one", async () => {
  const env = await openedShoppingList();
  env.net.route("GET", "/auth/me", me(86400, USER_B));
  env.net.route("GET", "/api/conversations", listBody([]));
  env.net.route("GET", "/api/favorites", H.favoritesBody([]));

  env.ctl.recheck();
  await env.settle();

  const view = env.view();
  assert.equal(view.screen, "app");
  assert.equal(view.currentId, null);
  assert.deepEqual(view.shopping.lists, []);
  assert.equal(view.shopping.current, null);
  assert.equal(view.shopping.draft, null);
});

scenario("a_draft_is_not_lost_when_the_page_asks_again_who_is_signed_in", async () => {
  const env = await withShopping();
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  env.ctl.toggleShoppingDraftAtHome(0, true);
  assert.equal(env.view().shopping.draft.remaining, 2);

  env.ctl.recheck(); // the tab became visible again: the same person, the server is asked afresh
  await env.settle();

  const view = env.view();
  assert.equal(view.screen, "app");
  assert.equal(view.section, "shopping");
  assert.equal(view.shopping.draft.remaining, 2, "the unread draft is exactly as it was");
  assert.equal(view.shopping.draft.ingredients[0].atHome, true);
  assert.equal(env.net.requests("POST", SHOP_URL).length, 0);
});

scenario("a_background_refresh_does_not_overwrite_a_state_change_in_flight", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  env.net.route("GET", LIST_URL_1, json(200, borshList()));
  await env.ctl.openShoppingList(H.LIST_1);
  const gate = deferred();
  env.net.route("PATCH", `${LIST_URL_1}/items/${H.ITEM_2}`, async () => {
    await gate.promise;
    return json(200, H.shoppingItem(H.ITEM_2, 1, "капуста", "300 г", "purchased"));
  });

  const changing = env.ctl.setShoppingItemState(H.ITEM_2, "purchased");
  await flush();
  env.ctl.showSection("shopping"); // the return to the lists, while the write is out
  await flush();
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 1, "the open list is not read over a write in flight");
  gate.resolve();
  await changing;
  await env.settle();

  assert.equal(env.view().shopping.current.counts.purchased, 1);
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 1);
});

scenario("a_list_read_overtaken_by_a_new_list_does_not_leave_the_panel_loading", async () => {
  const env = await withShopping();
  const gate = deferred();
  env.net.route("GET", SHOP_URL, async () => {
    await gate.promise;
    return H.shoppingListsBody([]);
  });
  env.net.route("POST", SHOP_URL, json(201, borshList()));

  env.ctl.showSection("shopping");
  await flush();
  assert.equal(env.view().shopping.status, "loading");
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  const saved = await env.ctl.saveShoppingDraft();
  assert.equal(saved.ok, true);
  gate.resolve();
  await env.settle();

  assert.deepEqual([env.view().shopping.status, env.view().shopping.lists.map((item) => item.id)], ["ready", [H.LIST_1]]);
  assert.equal(env.view().shopping.current.id, H.LIST_1);
});

scenario("a_shopping_list_read_that_fails_says_so_and_can_be_asked_again", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, fail(), H.shoppingListsBody([]));

  env.ctl.showSection("shopping");
  await env.settle();
  assert.deepEqual([env.view().shopping.status, env.view().shopping.error.text], ["error", Logic.TEXT.network]);
  assert.deepEqual(env.view().shopping.lists, []);

  await env.ctl.reloadShoppingLists();
  await env.settle();
  assert.deepEqual([env.view().shopping.status, env.view().shopping.lists], ["ready", []]);
  assert.equal(env.view().shopping.empty, true);
});

scenario("coming_back_to_the_page_while_a_list_is_open_reads_it_again", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, shoppingListBody(borshList()));
  env.net.route("GET", LIST_URL_1, json(200, borshList()));
  await env.ctl.openShoppingList(H.LIST_1);
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 1);

  env.ctl.recheck(); // the same person, checked with the server
  await env.settle();

  assert.equal(env.view().screen, "app");
  assert.equal(env.view().shopping.current.id, H.LIST_1);
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 2, "the open list is read from the server again");
  assert.equal(env.net.requests("GET", SHOP_URL).length, 1, "and the lists with it");
});

scenario("a_draft_with_everything_at_home_is_still_a_saveable_list", async () => {
  const env = await withShopping();
  env.net.route("POST", SHOP_URL, json(201, borshList("active", BORSH_ITEMS.map((item) => ({ ...item, state: "at_home" })))));

  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  for (let position = 0; position < 3; position++) {
    env.ctl.toggleShoppingDraftAtHome(position, true);
  }
  assert.equal(env.view().shopping.draft.remaining, 0);
  const result = await env.ctl.saveShoppingDraft();
  await env.settle();

  assert.equal(result.ok, true);
  assert.deepEqual(env.net.requests("POST", SHOP_URL)[0].body.item_states.map((state) => [state.position, state.state]), [
    [0, "at_home"],
    [1, "at_home"],
    [2, "at_home"],
  ]);
  assert.deepEqual(env.view().shopping.current.counts, { total: 3, unchecked: 0, at_home: 3, purchased: 0 });
});

scenario("only_identifiers_and_states_that_can_be_one_are_sent", async () => {
  const env = await withShopping();

  for (const source of [["x", 2, 0], [CONV_A, 0, 0], [CONV_A, 2, -1], [CONV_A, 2, 5], [CONV_A, "2", 0]]) {
    assert.equal((await env.ctl.beginShoppingDraftFromRecipe(...source)).reason, "invalid");
  }
  assert.equal(env.net.requests("GET", /recipes/).length, 0);

  const opened = await openedShoppingList();
  assert.deepEqual(await opened.ctl.setShoppingItemState(H.ITEM_2, "lost"), { ok: false, reason: "invalid" });
  assert.deepEqual(await opened.ctl.setShoppingItemState("not-an-id", "purchased"), { ok: false, reason: "invalid" });
  assert.equal(opened.net.requests("PATCH", /items/).length, 0);
});

// ============================================================================================
// The last draft protection: a save that is out, or whose answer was lost, is unsaved work
// ============================================================================================

scenario("a_draft_is_unsaved_while_its_save_is_out_or_in_doubt", async () => {
  const env = await withShopping();
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  assert.equal(env.ctl.shoppingDraftDirty(), false, "an untouched draft is not unsaved work");
  env.ctl.toggleShoppingDraftAtHome(0, true);
  assert.equal(env.ctl.shoppingDraftDirty(), true, "a mark is unsaved work");

  const gate = deferred();
  env.net.route("POST", SHOP_URL, async () => {
    await gate.promise;
    return json(201, borshList());
  });
  const saving = env.ctl.saveShoppingDraft();
  await flush();
  assert.equal(env.view().shopping.draft.phase, "saving");
  assert.equal(env.ctl.shoppingDraftDirty(), true, "the save is still out, so the draft is still unsaved");
  assert.equal(env.ctl.cancelShoppingDraft().ok, false, "the draft cannot be discarded while the save is out");

  gate.resolve();
  assert.equal((await saving).ok, true);
  await env.settle();
  assert.equal(env.ctl.shoppingDraftDirty(), false, "a confirmed save ends the question");

  // An answer that never came leaves the draft, with its key and frozen payload, for a retry.
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  env.ctl.toggleShoppingDraftAtHome(0, true);
  env.net.route("POST", SHOP_URL, fail());
  assert.deepEqual((await env.ctl.saveShoppingDraft()).reason, "uncertain");
  const draft = env.ctl._state().shopping.draft;
  assert.equal(draft.phase, "uncertain");
  assert.ok(draft.key, "the key is not thrown away");
  assert.ok(draft.payload, "the frozen payload is not thrown away");
  assert.equal(env.ctl.shoppingDraftDirty(), true, "a save of an unknown outcome is still unsaved work");

  assert.deepEqual(env.ctl.cancelShoppingDraft(), { ok: true, origin: "chats" });
  assert.equal(env.ctl.shoppingDraftDirty(), false, "an explicitly discarded draft ends the question");
});

scenario("an_unmarked_draft_is_unsaved_while_its_save_is_out_or_in_doubt", async () => {
  const env = await withShopping();
  const draft = () => env.ctl._state().shopping.draft;
  const unmarked = () => draft() !== null && !draft().atHome.some(Boolean);

  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  assert.equal(env.ctl.shoppingDraftDirty(), false, "an untouched draft is not unsaved work");

  // Nothing is marked, but the save is out: its key and its frozen payload are the only way to
  // learn whether the list was made, and leaving would take them with the page.
  const gate = deferred();
  env.net.route("POST", SHOP_URL, async () => {
    await gate.promise;
    return json(201, borshList("active", []));
  });
  const saving = env.ctl.saveShoppingDraft();
  await flush();
  assert.equal(draft().phase, "saving");
  assert.equal(unmarked(), true, "the draft is still unmarked");
  assert.equal(env.ctl.shoppingDraftDirty(), true, "an unmarked save that is out is unsaved work");
  assert.deepEqual(env.ctl.cancelShoppingDraft(), { ok: false, reason: "busy" }, "the draft cannot be discarded while the save is out");

  gate.resolve();
  assert.equal((await saving).ok, true);
  await env.settle();
  assert.equal(env.ctl.shoppingDraftDirty(), false, "a confirmed save ends the question");

  // An answer that never came leaves the unmarked draft, with its key and frozen payload.
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  env.net.route("POST", SHOP_URL, fail());
  assert.deepEqual((await env.ctl.saveShoppingDraft()).reason, "uncertain");
  assert.equal(draft().phase, "uncertain");
  assert.equal(unmarked(), true, "the draft is still unmarked");
  assert.ok(draft().key, "the key is not thrown away");
  assert.ok(draft().payload, "the frozen payload is not thrown away");
  assert.equal(env.ctl.shoppingDraftDirty(), true, "an unmarked save of an unknown outcome is unsaved work");

  // The retry with the same key settles it: nothing is left to warn about.
  env.net.route("POST", SHOP_URL, json(200, { ...borshList("active", []), created: false }));
  assert.equal((await env.ctl.retryShoppingDraft()).ok, true);
  await env.settle();
  assert.equal(env.ctl.shoppingDraftDirty(), false, "the confirmed retry ends the question");

  // An explicit discard of an unmarked draft whose save is in doubt ends it too.
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  env.net.route("POST", SHOP_URL, fail());
  assert.deepEqual((await env.ctl.saveShoppingDraft()).reason, "uncertain");
  assert.equal(env.ctl.shoppingDraftDirty(), true);
  assert.deepEqual(env.ctl.cancelShoppingDraft(), { ok: true, origin: "chats" });
  assert.equal(env.ctl.shoppingDraftDirty(), false, "an explicitly discarded draft ends the question");

  // A session that ends takes the unmarked draft and its save with it: nothing of it is unsaved
  // work, and the answer that comes later brings nothing back. (settle() would wait for the save
  // that is still out, so the page is only flushed while its answer is held.)
  await env.ctl.beginShoppingDraftFromRecipe(CONV_A, 2, 0);
  const late = deferred();
  env.net.route("POST", SHOP_URL, async () => {
    await late.promise;
    return json(201, borshList("active", []));
  });
  const savingEnded = env.ctl.saveShoppingDraft();
  await flush();
  assert.equal(env.ctl.shoppingDraftDirty(), true);
  env.net.route("GET", "/auth/me", notAuthenticated());
  env.ctl.recheck();
  await flush();
  assert.equal(env.view().screen, "login");
  assert.equal(env.ctl._state().shopping.draft, null);
  assert.equal(env.ctl.shoppingDraftDirty(), false, "nothing of the ended session is unsaved work");
  late.resolve();
  await savingEnded;
  await env.settle();
  assert.equal(env.ctl._state().shopping.draft, null, "the late answer does not bring the draft back");
  assert.equal(env.ctl.shoppingDraftDirty(), false);
});

// ============================================================================================
// The per-list read counts belong to one page of one person, and are released with it
// ============================================================================================

const OTHER_LIST = "dddddddd-dddd-4ddd-8ddd-ddddddddddd1";

function signInAs(env, userId, conversations, histories = {}) {
  env.net.route("GET", "/auth/me", me(86400, userId));
  env.net.route("GET", "/api/conversations", listBody(conversations));
  for (const conversationItem of conversations) {
    env.net.route(
      "GET",
      new RegExp("/api/conversations/" + conversationItem.id + "/messages(\\?|$)"),
      histories[conversationItem.id] || historyBody(conversationItem.id, [])
    );
  }
}

scenario("the_per_list_read_counts_are_released_with_the_account_and_do_not_accumulate", async () => {
  const env = await withShopping();
  const plov = H.shoppingDetail(H.LIST_2, "Плов", "active", [
    H.shoppingItem(H.ITEM_3, 0, "рис", "200 г", "unchecked"),
  ]);
  env.net.route("GET", SHOP_URL, H.shoppingListsBody([H.shoppingSummary(borshList()), H.shoppingSummary(plov)]));
  env.net.route("GET", LIST_URL_1, json(200, borshList()));
  env.net.route("GET", LIST_URL_2, json(200, plov));

  env.ctl.showSection("shopping");
  await env.settle();
  assert.deepEqual(await env.ctl.openShoppingList(H.LIST_1), { ok: true });
  assert.deepEqual(await env.ctl.openShoppingList(H.LIST_2), { ok: true });
  assert.deepEqual(
    env.ctl._state().shoppingDetailReads,
    { [H.LIST_1]: 1, [H.LIST_2]: 1 },
    "every list that was read has its own count"
  );

  // Another person signs in: nothing of the last person's counts is kept.
  signInAs(env, USER_B, []);
  env.ctl.recheck();
  await env.settle();
  assert.equal(env.view().screen, "app");
  assert.deepEqual(env.ctl._state().shoppingDetailReads, {}, "the counts are released with the account");

  // The first read of his page starts from nothing.
  const other = H.shoppingDetail(OTHER_LIST, "Суп", "active", [
    H.shoppingItem(H.ITEM_1, 0, "лук", "1 шт.", "unchecked"),
  ]);
  env.net.route("GET", SHOP_URL, H.shoppingListsBody([H.shoppingSummary(other)]));
  env.net.route("GET", `/api/shopping-lists/${OTHER_LIST}`, json(200, other));
  env.ctl.showSection("shopping");
  await env.settle();
  assert.deepEqual(await env.ctl.openShoppingList(OTHER_LIST), { ok: true });
  assert.deepEqual(env.ctl._state().shoppingDetailReads, { [OTHER_LIST]: 1 });

  // Back and forth twice: each page starts over, and no list of an earlier page is retained.
  signInAs(env, USER_A, [conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]),
  });
  env.ctl.recheck();
  await env.settle();
  assert.deepEqual(env.ctl._state().shoppingDetailReads, {});
  env.ctl.showSection("shopping");
  await env.settle();
  assert.deepEqual(await env.ctl.openShoppingList(H.LIST_1), { ok: true });
  assert.deepEqual(env.ctl._state().shoppingDetailReads, { [H.LIST_1]: 1 }, "her list is read as her page's first");

  signInAs(env, USER_B, []);
  env.ctl.recheck();
  await env.settle();
  assert.deepEqual(env.ctl._state().shoppingDetailReads, {});
  env.ctl.showSection("shopping");
  await env.settle();
  env.net.route("GET", `/api/shopping-lists/${OTHER_LIST}`, json(200, other));
  assert.deepEqual(await env.ctl.openShoppingList(OTHER_LIST), { ok: true });
  assert.deepEqual(
    env.ctl._state().shoppingDetailReads,
    { [OTHER_LIST]: 1 },
    "three account changes leave only the current page's own counts"
  );
});

scenario("a_read_of_the_previous_account_never_lands_in_the_next_ones_page", async () => {
  const env = await withShopping();
  env.net.route("GET", SHOP_URL, H.shoppingListsBody([H.shoppingSummary(borshList())]));
  const slow = deferred();
  env.net.route("GET", LIST_URL_1, async () => {
    await slow.promise;
    return json(200, borshList());
  });

  const reading = env.ctl.openShoppingList(H.LIST_1);
  await flush();
  assert.equal(env.net.requests("GET", LIST_URL_1).length, 1);
  assert.deepEqual(Object.keys(env.ctl._state().shoppingDetailReads), [H.LIST_1]);

  // Another person signs in while the read is still out.
  signInAs(env, USER_B, []);
  env.ctl.recheck();
  await flush(80); // (not settle: the previous person's read is deliberately still out)
  assert.equal(env.view().screen, "app");
  assert.deepEqual(env.ctl._state().shoppingDetailReads, {});

  // The old answer lands: it is applied to nothing and reported as stale.
  slow.resolve();
  assert.deepEqual(await reading, { ok: false, reason: "stale" });
  await env.settle();
  assert.equal(env.view().shopping.current, null);
  assert.deepEqual(env.view().shopping.lists, []);

  // Normal reads and writes work on the new page.
  const other = H.shoppingDetail(OTHER_LIST, "Суп", "active", [
    H.shoppingItem(H.ITEM_2, 0, "лук", "1 шт.", "unchecked"),
  ]);
  env.net.route("GET", SHOP_URL, H.shoppingListsBody([H.shoppingSummary(other)]));
  env.net.route("GET", `/api/shopping-lists/${OTHER_LIST}`, json(200, other));
  env.net.route(
    "PATCH",
    `/api/shopping-lists/${OTHER_LIST}/items/${H.ITEM_2}`,
    json(200, H.shoppingItem(H.ITEM_2, 0, "лук", "1 шт.", "purchased"))
  );
  env.ctl.showSection("shopping");
  await env.settle();
  assert.deepEqual(await env.ctl.openShoppingList(OTHER_LIST), { ok: true });
  assert.deepEqual(env.ctl._state().shoppingDetailReads, { [OTHER_LIST]: 1 });

  const changed = await env.ctl.setShoppingItemState(H.ITEM_2, "purchased");
  await env.settle();
  assert.equal(changed.ok, true);
  assert.equal(env.view().shopping.current.counts.purchased, 1, "a write works after the reset");
});

// ============================================================================================
// The profile
// ============================================================================================

scenario("the_profile_is_read_on_the_way_in_and_carries_its_two_settings_and_the_ways_in", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route(
    "GET",
    "/api/profile",
    H.profileBody("Елена", "large", [
      H.loginMethod("telegram"),
      H.loginMethod("email", "revoked", "elena@example.com"),
    ])
  );

  const view = await env.boot();

  assert.equal(view.screen, "app");
  assert.equal(view.profile.status, "ready");
  assert.equal(view.profile.loaded, true);
  assert.equal(view.profile.displayName, "Елена");
  assert.equal(view.profile.textSize, "large");
  assert.deepEqual(view.profile.loginMethods, [
    { provider: "telegram", address: null, status: "active" },
    { provider: "email", address: "elena@example.com", status: "revoked" },
  ]);
  assert.equal(view.profile.editing, false, "the pane opens on the saved values");
  assert.equal(view.profile.saving, false);
});

scenario("a_profile_that_cannot_be_read_does_not_hold_up_the_conversations", async () => {
  const env = makeEnv().signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Старый вопрос"), message(2, "assistant", "Старый ответ")]),
  });
  env.net.route("GET", "/api/profile", fail(), H.profileBody("Елена", "large"));

  const view = await env.boot();

  assert.equal(view.screen, "app");
  assert.equal(view.list.status, "ready");
  assert.equal(view.currentId, CONV_A);
  assert.equal(view.thread.items.length, 2);
  assert.equal(view.profile.status, "error");
  assert.equal(view.profile.loaded, false);
  assert.equal(view.profile.textSize, "normal", "the browser's own size is the fallback");
  assert.ok(/[А-Яа-я]/.test(view.profile.error.text));

  await env.ctl.reloadProfile();
  await env.settle();

  assert.equal(env.view().profile.status, "ready");
  assert.equal(env.view().profile.textSize, "large");
  assert.equal(env.view().profile.error, null);
});

scenario("a_profile_read_that_loses_the_session_follows_the_same_auth_loss_flow", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/api/profile", notAuthenticated());

  const view = await env.boot();

  assert.equal(view.screen, "login");
  assert.equal(view.notice.text, Logic.TEXT.authExpired);
  assert.equal(view.profile.textSize, "normal");
  assert.equal(view.profile.loaded, false);
});

scenario("a_profile_answer_that_is_not_the_applications_is_an_error_in_that_pane_only", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/api/profile", json(200, { display_name: "Елена", text_size: "huge", login_methods: [] }));

  const view = await env.boot();

  assert.equal(view.screen, "app");
  assert.equal(view.profile.status, "error");
  assert.equal(view.profile.loaded, false);
  assert.equal(view.profile.textSize, "normal");
});

scenario("saving_the_profile_sends_its_two_own_fields_and_updates_the_view", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.ctl.beginProfileEdit();
  assert.equal(env.view().profile.editing, true);
  env.net.route("PATCH", "/api/profile", H.profileBody("Иван", "large"));

  const result = await env.ctl.saveProfile({ displayName: "  Иван  ", textSize: "large" });
  await env.settle();

  assert.deepEqual(result, { ok: true });
  assert.deepEqual(env.net.requests("PATCH", "/api/profile")[0].body, {
    display_name: "Иван",
    text_size: "large",
  });
  const view = env.view();
  assert.equal(view.profile.displayName, "Иван");
  assert.equal(view.profile.textSize, "large");
  assert.deepEqual(view.profile.notice, { kind: "ok", text: Logic.TEXT.profileSaved });
  assert.equal(view.profile.fieldError, null);
  assert.equal(view.profile.editing, false, "a successful save returns to the saved values");
  assert.equal(view.profile.draft, null);
  assert.ok(env.announced.includes(Logic.TEXT.profileSaved));
});

scenario("an_unchanged_profile_form_sends_no_patch_and_returns_to_the_saved_values", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/api/profile", H.profileBody("Елена", "normal"));
  await env.boot();
  const asked = env.net.log.length;

  env.ctl.beginProfileEdit();
  assert.equal(env.view().profile.editing, true);
  // Space around the saved name is not a change: the server trims it away.
  const result = await env.ctl.saveProfile({ displayName: "  Елена  ", textSize: "normal" });
  await env.settle();

  assert.deepEqual(result, { ok: true, unchanged: true });
  assert.equal(env.net.requests("PATCH", "/api/profile").length, 0, "nothing was written");
  assert.equal(env.net.log.length, asked, "nothing was asked");
  const view = env.view();
  assert.equal(view.profile.editing, false);
  assert.equal(view.profile.draft, null);
  assert.equal(view.profile.notice, null, "no save is claimed");
  assert.equal(view.profile.displayName, "Елена");
  assert.equal(view.profile.textSize, "normal");
});

scenario("a_failed_profile_save_leaves_the_form_open_with_what_was_typed", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/api/profile", H.profileBody("Елена", "normal"));
  await env.boot();
  env.net.route("PATCH", "/api/profile", fail());

  env.ctl.beginProfileEdit();
  const result = await env.ctl.saveProfile({ displayName: "Новое имя", textSize: "large" });
  await env.settle();

  assert.deepEqual([result.ok, result.reason], [false, "unavailable"]);
  const view = env.view();
  assert.equal(view.profile.editing, true, "the form stays open");
  assert.deepEqual(view.profile.draft, {
    displayName: "Новое имя",
    textSize: "large",
    measures: { glass_ml: "", teaspoon_ml: "", tablespoon_ml: "" },
  });
  assert.equal(view.profile.textSize, "normal", "the unsaved size is not applied");
  assert.equal(view.profile.displayName, "Елена", "the saved name is unchanged");
  assert.equal(view.profile.notice.kind, "error");
});

scenario("a_profile_name_is_checked_before_anything_is_sent", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  const asked = env.net.log.length;

  for (const value of ["", "   ", "x".repeat(201), "😀".repeat(201), "a​b", "a‮b", "a\nb", "a\u0007b"]) {
    const result = await env.ctl.saveProfile({ displayName: value, textSize: "normal" });
    assert.deepEqual([result.ok, result.reason], [false, "invalid"], JSON.stringify(value));
    assert.ok(/[А-Яа-я]/.test(env.view().profile.fieldError), JSON.stringify(value));
    assert.equal(env.view().profile.editing, true, "a refusal keeps the form open");
  }

  assert.equal(env.net.requests("PATCH", "/api/profile").length, 0);
  assert.equal(env.net.log.length, asked, "nothing was asked");
});

scenario("a_name_the_server_refuses_is_shown_under_the_name_field", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.net.route("PATCH", "/api/profile", apiError(400, "invalid_display_name"));

  const result = await env.ctl.saveProfile({ displayName: "Иван", textSize: "normal" });
  await env.settle();

  assert.deepEqual([result.ok, result.reason], [false, "rejected"]);
  assert.equal(env.view().profile.fieldError, Logic.CODE_MESSAGES.invalid_display_name);
  assert.equal(env.view().profile.notice, null);
  assert.equal(env.view().profile.editing, true);
});

scenario("a_profile_size_the_server_refuses_is_a_notice_not_a_name_error", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.net.route("PATCH", "/api/profile", apiError(400, "invalid_text_size"));

  const result = await env.ctl.saveProfile({ displayName: "Иван", textSize: "large" });
  await env.settle();

  assert.deepEqual([result.ok, result.reason], [false, "rejected"]);
  assert.equal(env.view().profile.fieldError, null);
  assert.deepEqual(env.view().profile.notice.kind, "error");
  assert.equal(env.view().profile.notice.text, Logic.CODE_MESSAGES.invalid_text_size);
  assert.equal(env.view().profile.editing, true);
});

scenario("a_profile_save_that_loses_the_session_follows_the_same_auth_loss_flow", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.net.route("PATCH", "/api/profile", notAuthenticated());

  const result = await env.ctl.saveProfile({ displayName: "Иван", textSize: "large" });
  await env.settle();

  assert.deepEqual([result.ok, result.reason], [false, "auth"]);
  assert.equal(env.view().screen, "login");
  assert.equal(env.view().profile.textSize, "normal");
  assert.equal(env.view().profile.loaded, false);
  assert.equal(env.view().profile.editing, false);
});

scenario("opening_the_profile_reads_it_again_and_a_failed_read_keeps_the_last_known_one", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/api/profile", H.profileBody("Елена", "large"), fail());
  await env.boot();
  const asked = env.net.requests("GET", "/api/profile").length;

  env.ctl.showSection("profile");
  await env.settle();
  env.ctl.showSection("chats");
  env.ctl.showSection("profile");
  await env.settle();

  assert.equal(env.net.requests("GET", "/api/profile").length, asked + 2);
  assert.equal(env.view().section, "profile");
  assert.equal(env.view().profile.status, "error");
  assert.equal(env.view().profile.displayName, "Елена", "what was known stays readable");
  assert.equal(env.view().profile.textSize, "large");
  assert.equal(env.view().profile.editing, false, "returning to the pane shows the saved values");
});

scenario("another_person_never_gets_the_last_ones_profile", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/api/profile", H.profileBody("Анна", "large"));
  await env.boot();
  assert.equal(env.view().profile.displayName, "Анна");

  signInAs(env, USER_B, []);
  env.net.route("GET", "/api/profile", H.profileBody("Борис", "normal"));
  env.ctl.recheck();
  await env.settle();

  assert.equal(env.view().screen, "app");
  assert.equal(env.view().profile.displayName, "Борис");
  assert.equal(env.view().profile.textSize, "normal");
  assert.equal(env.view().profile.editing, false);
  assert.ok(!JSON.stringify(env.view().profile).includes("Анна"));
});

// A read that was already out when a save was accepted answers with the server's values from
// before that save: it must not put them back over the saved ones (or over the save's message).
scenario("a_profile_read_that_was_out_before_a_save_cannot_put_the_old_values_back", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();

  const readReached = deferred();
  const releaseRead = deferred();
  env.net.route(
    "GET",
    "/api/profile",
    async () => {
      readReached.resolve();
      await releaseRead.promise;
      return H.profileBody("Елена", "normal"); // what the server said before the save
    }
  );
  const reading = env.ctl.reloadProfile();
  await readReached.promise;
  assert.equal(env.view().profile.status, "loading");

  env.net.route("PATCH", "/api/profile", H.profileBody("Иван", "large"));
  const saved = await env.ctl.saveProfile({ displayName: "Иван", textSize: "large" });
  await flush();
  assert.deepEqual(
    [saved.ok, env.view().profile.displayName, env.view().profile.textSize],
    [true, "Иван", "large"]
  );

  releaseRead.resolve();
  assert.equal(await reading, false, "the held answer is older than the save and is discarded");
  await env.settle();

  const view = env.view();
  assert.equal(view.profile.status, "ready");
  assert.equal(view.profile.displayName, "Иван", "the read may not restore what the save replaced");
  assert.equal(view.profile.textSize, "large");
  assert.deepEqual(
    view.profile.notice,
    { kind: "ok", text: Logic.TEXT.profileSaved },
    "the save's message stays with the values it saved"
  );
  assert.equal(view.profile.draft, null);
  assert.equal(view.profile.editing, false);
});

// A read begun while the save is still pending may be answered from either side of the
// server's write. Whatever it says, it may not replace the values the save made true.
scenario("a_profile_read_begun_while_a_save_is_pending_cannot_replace_the_saved_values", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();

  const patchReached = deferred();
  const releasePatch = deferred();
  env.net.route(
    "PATCH",
    "/api/profile",
    async () => {
      patchReached.resolve();
      await releasePatch.promise;
      return H.profileBody("Иван", "large");
    }
  );
  const saving = env.ctl.saveProfile({ displayName: "Иван", textSize: "large" });
  await patchReached.promise;
  assert.equal(env.view().profile.saving, true);

  const readReached = deferred();
  const releaseRead = deferred();
  env.net.route(
    "GET",
    "/api/profile",
    async () => {
      readReached.resolve();
      await releaseRead.promise;
      return H.profileBody("Елена", "normal"); // the answer the server made before the write
    }
  );
  const reading = env.ctl.reloadProfile();
  await readReached.promise;

  releasePatch.resolve();
  assert.equal((await saving).ok, true);
  await flush();
  assert.deepEqual(
    [env.view().profile.displayName, env.view().profile.textSize],
    ["Иван", "large"]
  );

  releaseRead.resolve();
  assert.equal(await reading, false, "the read began before the save was accepted");
  await env.settle();

  const view = env.view();
  assert.equal(view.profile.status, "ready");
  assert.equal(view.profile.displayName, "Иван", "the pending save's values stay");
  assert.equal(view.profile.textSize, "large");
  assert.deepEqual(view.profile.notice, { kind: "ok", text: Logic.TEXT.profileSaved });
  assert.equal(view.profile.editing, false);
});

// Two reads overlap and answer in the opposite order: only the newer one may be shown, so an
// older answer cannot regress the pane to a snapshot the newer one has already replaced.
scenario("overlapping_profile_reads_apply_only_the_newest_answer", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();

  const firstReached = deferred();
  const releaseFirst = deferred();
  const secondReached = deferred();
  const releaseSecond = deferred();
  env.net.route(
    "GET",
    "/api/profile",
    async () => {
      firstReached.resolve();
      await releaseFirst.promise;
      return H.profileBody("Первый", "normal");
    },
    async () => {
      secondReached.resolve();
      await releaseSecond.promise;
      return H.profileBody("Второй", "large");
    }
  );

  const first = env.ctl.reloadProfile();
  await firstReached.promise;
  const second = env.ctl.reloadProfile();
  await secondReached.promise;

  releaseSecond.resolve();
  assert.equal(await second, true);
  await flush();
  assert.deepEqual(
    [env.view().profile.displayName, env.view().profile.textSize],
    ["Второй", "large"]
  );

  releaseFirst.resolve();
  assert.equal(await first, false, "the older answer arrived last and is not the authority");
  await env.settle();

  const view = env.view();
  assert.equal(view.profile.status, "ready");
  assert.equal(view.profile.displayName, "Второй", "the older snapshot must not come back");
  assert.equal(view.profile.textSize, "large");
});

// A save that fails changes nothing on the server, so it neither changes the values nor
// blocks a later read; what was typed stays in the draft for a retry until a read replaces it.
scenario("a_failed_profile_save_leaves_the_values_and_a_fresh_read_still_applies", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.net.route("PATCH", "/api/profile", fail());

  const failed = await env.ctl.saveProfile({ displayName: "Иван", textSize: "large" });
  await flush();

  assert.deepEqual([failed.ok, failed.reason], [false, "unavailable"]);
  assert.equal(env.view().profile.displayName, "Елена");
  assert.equal(env.view().profile.textSize, "normal");
  assert.deepEqual(env.view().profile.draft, {
    displayName: "Иван",
    textSize: "large",
    measures: { glass_ml: "", teaspoon_ml: "", tablespoon_ml: "" },
  });
  assert.equal(env.view().profile.notice.kind, "error");

  env.net.route("GET", "/api/profile", H.profileBody("Пётр", "large"));
  const reloaded = await env.ctl.reloadProfile();
  await env.settle();

  assert.equal(reloaded, true);
  const view = env.view();
  assert.equal(view.profile.status, "ready");
  assert.equal(view.profile.displayName, "Пётр");
  assert.equal(view.profile.textSize, "large");
  assert.equal(view.profile.error, null);
  assert.equal(view.profile.notice, null);
  assert.equal(view.profile.draft, null);
  assert.equal(view.profile.editing, false, "a fresh read shows the saved values");
});

// The ordering above only discards answers that are stale; a read that is still the newest
// keeps the existing auth-loss flow: a 401 ends the session and takes the profile with it.
scenario("a_profile_read_begun_after_a_save_still_ends_the_session_on_401", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.net.route("PATCH", "/api/profile", H.profileBody("Иван", "large"));
  assert.equal((await env.ctl.saveProfile({ displayName: "Иван", textSize: "large" })).ok, true);
  await flush();
  assert.equal(env.view().profile.displayName, "Иван");

  env.net.route("GET", "/api/profile", notAuthenticated());
  assert.equal(await env.ctl.reloadProfile(), false);
  await env.settle();

  const view = env.view();
  assert.equal(view.screen, "login");
  assert.equal(view.notice.text, Logic.TEXT.authExpired);
  assert.equal(view.profile.loaded, false);
  assert.equal(view.profile.displayName, null);
});

scenario("logging_out_after_a_profile_save_still_leaves_nothing_of_the_profile", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.net.route("PATCH", "/api/profile", H.profileBody("Иван", "large"));
  assert.equal((await env.ctl.saveProfile({ displayName: "Иван", textSize: "large" })).ok, true);
  await flush();
  assert.equal(env.view().profile.displayName, "Иван");

  env.net.route("POST", "/auth/logout", text(204, null));
  assert.equal(await env.ctl.logout(), true);
  await env.settle();

  const view = env.view();
  assert.equal(view.screen, "login");
  assert.equal(view.notice.text, Logic.TEXT.loggedOut);
  assert.equal(view.profile.loaded, false);
  assert.equal(view.profile.displayName, null);
  assert.ok(!JSON.stringify(view.profile).includes("Иван"));
});

// The explanation of the data is checked against the lifecycle tests/test_admin_retention.py
// verifies: messages and their photos go one by one, RETENTION_DAYS after their own creation;
// a conversation is not deleted for being old, and an old one with a newer message stays.
scenario("the_profile_data_note_describes_message_retention_and_conversation_cleanup", async () => {
  const messages = Logic.TEXT.profileDataMessages;
  const recipes = Logic.TEXT.profileDataRecipes;

  assert.match(messages, /Сообщения и фотографии к ним хранятся 365 дней с момента создания/);
  assert.match(messages, /Разговор не удаляется только из-за возраста/);
  assert.match(messages, /старый разговор, в котором есть более новые сообщения, остается/);
  assert.match(messages, /не осталось сообщений и им не пользовались 365 дней/);
  assert.doesNotMatch(messages, /^Разговоры, сообщения и фотографии к ним хранятся/);

  assert.match(recipes, /Ответы помощника с рецептами — это сообщения/);
  assert.match(recipes, /каждый хранится 365 дней с момента создания/);
  assert.match(recipes, /даже если сам разговор остается/);
  assert.match(recipes, /[Рр]ецепты, сохраненные в избранное, хранятся, пока вы сами их не удалите/);
});

// ============================================================================================
// KH-F01-B1: the profile's household measures
// ============================================================================================

scenario("the_profile_read_carries_the_three_measures_with_their_effective_values", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/api/profile", H.profileBody("Елена", "normal", [], { glass_ml: 240 }));

  const view = await env.boot();

  assert.deepEqual(view.profile.measures, {
    glass_ml: { override: 240, effective: 240, defaultMl: 250, min: 50, max: 1000 },
    teaspoon_ml: { override: null, effective: 5, defaultMl: 5, min: 1, max: 20 },
    tablespoon_ml: { override: null, effective: 15, defaultMl: 15, min: 5, max: 50 },
  });
});

scenario("a_profile_answer_with_an_unusable_measures_block_is_not_shown", async () => {
  const good = () => H.profileBody("Елена", "normal", []).body;
  const cases = [];
  const missing = good();
  delete missing.measures;
  cases.push(missing);
  cases.push({ ...good(), measures: null });
  cases.push({ ...good(), measures: H.profileMeasures({ glass_ml: 40 }) });
  cases.push({
    ...good(),
    measures: {
      ...H.profileMeasures(),
      glass_ml: { override: 240, effective: 250, default: 250, min: 50, max: 1000 },
    },
  });
  cases.push({
    ...good(),
    measures: { ...H.profileMeasures(), glass_ml: { override: null, effective: 250, default: 300, min: 50, max: 1000 } },
  });
  cases.push({
    ...good(),
    measures: { ...H.profileMeasures(), glass_ml: { override: "240", effective: 240, default: 250, min: 50, max: 1000 } },
  });

  for (const body of cases) {
    const env = makeEnv().signedIn([]);
    env.net.route("GET", "/api/profile", H.json(200, body));
    const view = await env.boot();
    assert.equal(view.profile.status, "error", JSON.stringify(body.measures));
    assert.equal(view.profile.loaded, false);
    assert.equal(view.profile.measures, null);
  }
});

scenario("only_the_changed_profile_fields_are_sent", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.ctl.beginProfileEdit();
  env.net.route("PATCH", "/api/profile", H.profileBody("Елена", "normal", [], { glass_ml: 240 }));

  const result = await env.ctl.saveProfile({
    displayName: "Елена",
    textSize: "normal",
    measures: { glass_ml: "240", teaspoon_ml: "", tablespoon_ml: "" },
  });
  await env.settle();

  assert.deepEqual(result, { ok: true });
  assert.deepEqual(env.net.requests("PATCH", "/api/profile")[0].body, {
    measures: { glass_ml: 240 },
  });
  assert.equal(env.view().profile.measures.glass_ml.effective, 240);
});

scenario("changing_the_name_and_one_measure_sends_only_those_two_fields", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.ctl.beginProfileEdit();
  env.net.route(
    "PATCH",
    "/api/profile",
    H.profileBody("Иван", "normal", [], { tablespoon_ml: 20 })
  );

  const result = await env.ctl.saveProfile({
    displayName: "Иван",
    textSize: "normal",
    measures: { glass_ml: "", teaspoon_ml: "", tablespoon_ml: "20" },
  });
  await env.settle();

  assert.deepEqual(result, { ok: true });
  assert.deepEqual(env.net.requests("PATCH", "/api/profile")[0].body, {
    display_name: "Иван",
    measures: { tablespoon_ml: 20 },
  });
});

scenario("an_empty_measure_field_restores_that_default", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/api/profile", H.profileBody("Елена", "normal", [], { glass_ml: 240 }));
  await env.boot();
  env.ctl.beginProfileEdit();
  env.net.route("PATCH", "/api/profile", H.profileBody("Елена", "normal"));

  const result = await env.ctl.saveProfile({
    displayName: "Елена",
    textSize: "normal",
    measures: { glass_ml: "", teaspoon_ml: "", tablespoon_ml: "" },
  });
  await env.settle();

  assert.deepEqual(result, { ok: true });
  assert.deepEqual(env.net.requests("PATCH", "/api/profile")[0].body, {
    measures: { glass_ml: null },
  });
  assert.equal(env.view().profile.measures.glass_ml.override, null);
});

scenario("a_measure_field_that_only_looks_different_sends_no_patch", async () => {
  const env = makeEnv().signedIn([]);
  env.net.route("GET", "/api/profile", H.profileBody("Елена", "normal", [], { glass_ml: 240 }));
  await env.boot();
  const asked = env.net.log.length;
  env.ctl.beginProfileEdit();

  // "0240" is the same whole-millilitre value as the saved 240.
  const result = await env.ctl.saveProfile({
    displayName: "Елена",
    textSize: "normal",
    measures: { glass_ml: "0240", teaspoon_ml: "", tablespoon_ml: "" },
  });
  await env.settle();

  assert.deepEqual(result, { ok: true, unchanged: true });
  assert.equal(env.net.requests("PATCH", "/api/profile").length, 0);
  assert.equal(env.net.log.length, asked);
  assert.equal(env.view().profile.editing, false);
});

scenario("an_unusable_measure_is_refused_before_anything_is_sent_and_the_draft_stays", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.ctl.beginProfileEdit();
  const asked = env.net.log.length;

  for (const value of ["1.5", "abc", "49", "1001", "-5", "1e3", " 2 4 "]) {
    const result = await env.ctl.saveProfile({
      displayName: "Новое имя",
      textSize: "normal",
      measures: { glass_ml: value, teaspoon_ml: "", tablespoon_ml: "" },
    });
    assert.deepEqual([result.ok, result.reason], [false, "invalid"], value);
    const view = env.view();
    assert.ok(/[А-Яа-я]/.test(view.profile.measureError), value);
    assert.equal(view.profile.editing, true);
    assert.equal(view.profile.draft.displayName, "Новое имя", "the name typed is kept too");
    assert.equal(view.profile.draft.measures.glass_ml, value);
  }

  assert.equal(env.net.requests("PATCH", "/api/profile").length, 0);
  assert.equal(env.net.log.length, asked);
});

scenario("a_measure_the_server_refuses_is_a_measure_error_and_the_draft_stays", async () => {
  const env = makeEnv().signedIn([]);
  await env.boot();
  env.ctl.beginProfileEdit();
  env.net.route("PATCH", "/api/profile", H.apiError(400, "invalid_measure"));

  const result = await env.ctl.saveProfile({
    displayName: "Елена",
    textSize: "normal",
    measures: { glass_ml: "240", teaspoon_ml: "", tablespoon_ml: "" },
  });
  await env.settle();

  assert.deepEqual([result.ok, result.reason], [false, "rejected"]);
  const view = env.view();
  assert.equal(view.profile.measureError, Logic.CODE_MESSAGES.invalid_measure);
  assert.equal(view.profile.fieldError, null);
  assert.equal(view.profile.editing, true);
  assert.equal(view.profile.draft.measures.glass_ml, "240");
  assert.equal(view.profile.measures.glass_ml.override, null);
});

scenario("the_page_words_a_fraction_and_its_personal_unit_correctly", async () => {
  assert.equal(Logic.formatMeasure(4, 5, "glass"), "0,8 вашего стакана");
  assert.equal(Logic.formatMeasure(19, 20, "glass"), "0,95 вашего стакана");
  assert.equal(Logic.formatMeasure(1, 4, "glass"), "¼ вашего стакана");
  assert.equal(Logic.formatMeasure(3, 2, "tablespoon"), "1½ вашей столовой ложки");
  assert.equal(Logic.formatMeasure(5, 4, "teaspoon"), "1¼ вашей чайной ложки");
  assert.equal(Logic.formatMeasure(1, 1, "glass"), "1 ваш стакан");
  assert.equal(Logic.formatMeasure(2, 1, "glass"), "2 ваших стакана");
  assert.equal(Logic.formatMeasure(5, 1, "glass"), "5 ваших стаканов");
  assert.equal(Logic.formatMeasure(11, 1, "glass"), "11 ваших стаканов");
  assert.equal(Logic.formatMeasure(21, 1, "glass"), "21 ваш стакан");
  assert.equal(Logic.formatMeasure(44, 25, "glass"), "1,76 вашего стакана");
  assert.equal(Logic.formatMeasure(1, 0, "glass"), null, "a zero denominator is not a value");
  assert.equal(Logic.formatMeasure(1, 2, "cup"), null, "an unknown unit is not invented");
});

// ============================================================================================
// KH-F01-B1: the favorite detail's measures block
// ============================================================================================

function measuresBlock(extra = {}) {
  return {
    target: { glass_ml: 250, teaspoon_ml: 5, tablespoon_ml: 15 },
    source_glass_ml: null,
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
    ...extra,
  };
}

async function withMeasuredFavorite(block = measuresBlock(), extra = {}) {
  const item = structuredFavoriteItem({ measures: block, ...extra });
  const env = makeEnv({ structuredRecipes: true }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что?"), structuredAnswer()]),
  });
  env.net.route("GET", FAVORITES_URL, H.favoritesBody([item]));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(item, STRUCTURED_TEXT));
  await env.boot();
  await env.ctl.openFavorite(H.FAV_1);
  await env.settle();
  return { env, item };
}

scenario("a_structured_favorite_carries_the_conversion_block_as_the_page_reads_it", async () => {
  const block = measuresBlock({
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
    ],
  });
  const { env } = await withMeasuredFavorite(block, { ingredients: BORSH_RECIPE.ingredients });

  const current = env.view().favorites.current;
  assert.equal(current.status, "ready");
  assert.equal(current.measures.target.glass_ml, 250);
  assert.equal(current.measures.sourceGlassRequired, true);
  assert.deepEqual(current.measures.ingredients, [
    {
      position: 0,
      source: "1 стакан 200 мл",
      sourceKind: "household",
      targetUnit: "glass",
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
      sourceKind: "household",
      targetUnit: "tablespoon",
      status: "nominal_household",
      reason: "converted",
      exact: { numerator: 3, denominator: 2 },
      rounded: {
        numerator: 3,
        denominator: 2,
        relativeError: { numerator: 0, denominator: 1 },
      },
      identical: false,
      presentation: "rounded",
    },
  ]);
  // The original stored amounts are untouched.
  assert.deepEqual(current.ingredients, BORSH_RECIPE.ingredients);
});

scenario("a_malformed_conversion_block_is_ignored_and_the_recipe_stays_readable", async () => {
  const broken = [
    "not an object",
    { target: { glass_ml: 250, teaspoon_ml: 5 }, source_glass_required: false, ingredients: [] },
    { target: { glass_ml: 250, teaspoon_ml: 5, tablespoon_ml: 15 }, source_glass_required: "yes", ingredients: [] },
    {
      ...measuresBlock(),
      ingredients: [
        {
          ...measuresBlock().ingredients[0],
          exact: { numerator: 1.5, denominator: 2 },
          presentation: "exact",
        },
      ],
    },
    {
      ...measuresBlock(),
      ingredients: [
        {
          ...measuresBlock().ingredients[0],
          position: 5, // beyond the two ingredients of the recipe
        },
      ],
    },
  ];
  for (const block of broken) {
    const { env } = await withMeasuredFavorite(block, { ingredients: BORSH_RECIPE.ingredients });
    const current = env.view().favorites.current;
    assert.equal(current.status, "ready", JSON.stringify(block));
    assert.equal(current.measures, null, JSON.stringify(block));
    assert.deepEqual(current.ingredients, BORSH_RECIPE.ingredients);
  }
});

scenario("a_source_glass_choice_is_a_query_parameter_kept_for_the_open_recipe_only", async () => {
  const first = structuredFavoriteItem({ measures: measuresBlock() });
  const second = H.structuredFavorite(H.FAV_2, 2, "Уха", 1, 0, {
    servings: null,
    ingredients: UKHA_RECIPE.ingredients,
    steps: UKHA_RECIPE.steps,
    version: 1,
    measures: measuresBlock({ source_glass_required: false, ingredients: [] }),
  });
  const env = makeEnv({ structuredRecipes: true }).signedIn([conversation(CONV_A, 0)], {
    [CONV_A]: historyBody(CONV_A, [message(1, "user", "Что?"), structuredAnswer()]),
  });
  env.net.route("GET", FAVORITES_URL, H.favoritesBody([first, second]));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(first, STRUCTURED_TEXT));
  env.net.route("GET", FAV_URL_2, H.favoriteDetail(second, "Уха"));
  await env.boot();
  await env.ctl.openFavorite(H.FAV_1);
  await env.settle();
  assert.equal(env.view().favorites.current.sourceGlass, null);

  const chosen = structuredFavoriteItem({
    measures: measuresBlock({ source_glass_ml: 200 }),
  });
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(chosen, STRUCTURED_TEXT));
  await env.ctl.setSourceGlass(200);
  await env.settle();

  assert.equal(
    env.net.requests("GET", FAV_URL_1).slice(-1)[0].url,
    FAV_URL_1 + "?source_glass_ml=200"
  );
  assert.equal(env.view().favorites.current.measures.sourceGlassMl, 200);
  assert.equal(env.view().favorites.current.sourceGlass, 200);

  // Returning to the section refreshes the same recipe with the same choice.
  env.ctl.showSection("favorites");
  await env.settle();
  assert.equal(
    env.net.requests("GET", FAV_URL_1).slice(-1)[0].url,
    FAV_URL_1 + "?source_glass_ml=200"
  );

  // A different recipe starts without a choice.
  await env.ctl.openFavorite(H.FAV_2);
  await env.settle();
  assert.equal(env.view().favorites.current.sourceGlass, null);
  assert.equal(env.net.requests("GET", FAV_URL_2).slice(-1)[0].url, FAV_URL_2);
});

scenario("an_unusable_source_glass_choice_is_refused_without_a_request", async () => {
  const { env } = await withMeasuredFavorite();
  const asked = env.net.log.length;

  for (const value of ["49", "1001", "", "abc", "200.5", "-5"]) {
    const result = await env.ctl.setSourceGlass(value);
    assert.deepEqual(result, { ok: false, reason: "invalid" }, value);
  }

  assert.equal(env.net.log.length, asked);
  assert.equal(env.view().favorites.current.sourceGlass, null);
});

// A confirmed clarification and the block calculated for it: four fifths of the person's
// 250 mL glass, which is what "1 стакан" means once the source is said to be 200 mL.
function convertedGlassBlock(glass, extra = {}) {
  return measuresBlock({
    source_glass_ml: glass,
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
    ...extra,
  });
}

function chosenFavorite(block, extra = {}) {
  return structuredFavoriteItem({ measures: block, ...extra });
}

async function withConfirmedGlass(glass = 200) {
  const { env } = await withMeasuredFavorite();
  const chosen = chosenFavorite(convertedGlassBlock(glass));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(chosen, STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.setSourceGlass(glass), { ok: true });
  await env.settle();
  return env;
}

scenario("a_new_source_glass_choice_replaces_the_confirmed_one_with_its_own_block", async () => {
  const env = await withConfirmedGlass(200);
  let current = env.view().favorites.current;
  assert.equal(current.sourceGlass, 200);
  assert.equal(current.measures.sourceGlassMl, 200);
  assert.deepEqual(current.measures.ingredients[0].exact, { numerator: 4, denominator: 5 });
  assert.equal(current.sourceGlassPending, null);
  assert.equal(current.sourceGlassError, null);

  // 250 mL is exactly the person's glass: the new block says so (identity, nothing to
  // repeat), and the 200 mL conversion must not remain beside the new choice.
  const second = chosenFavorite(
    measuresBlock({
      source_glass_ml: 250,
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
    })
  );
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(second, STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.setSourceGlass(250), { ok: true });
  await env.settle();

  current = env.view().favorites.current;
  assert.equal(current.sourceGlass, 250);
  assert.equal(current.measures.sourceGlassMl, 250);
  assert.equal(current.measures.ingredients[0].identical, true);
  assert.equal(current.sourceGlassPending, null);
  assert.equal(current.sourceGlassError, null);
  assert.equal(
    env.net.requests("GET", FAV_URL_1).slice(-1)[0].url,
    FAV_URL_1 + "?source_glass_ml=250"
  );
});

scenario("a_failed_source_glass_choice_keeps_the_confirmed_one_and_says_so", async () => {
  const env = await withConfirmedGlass(200);
  env.net.route("GET", FAV_URL_1, fail());

  assert.deepEqual(await env.ctl.setSourceGlass(250), { ok: false, reason: "failed" });
  await env.settle();

  const current = env.view().favorites.current;
  assert.equal(current.status, "ready");
  // The confirmed choice and its conversion are exactly as they were: nothing shows 250
  // beside a block calculated for 200.
  assert.equal(current.sourceGlass, 200);
  assert.equal(current.measures.sourceGlassMl, 200);
  assert.deepEqual(current.measures.ingredients[0].exact, { numerator: 4, denominator: 5 });
  assert.equal(current.sourceGlassPending, null);
  assert.equal(current.sourceGlassError.value, 250);
  assert.equal(current.sourceGlassError.text, Logic.TEXT.sourceGlassFailed);
  assert.equal(current.sourceGlassError.correlationId, null);
});

scenario("a_failed_source_glass_choice_can_be_asked_for_again", async () => {
  const env = await withConfirmedGlass(200);
  env.net.route("GET", FAV_URL_1, fail());
  assert.deepEqual(await env.ctl.setSourceGlass(250), { ok: false, reason: "failed" });
  await env.settle();
  assert.equal(env.view().favorites.current.sourceGlassError.value, 250);

  const second = chosenFavorite(convertedGlassBlock(250));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(second, STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.setSourceGlass(250), { ok: true });
  await env.settle();

  const current = env.view().favorites.current;
  assert.equal(current.sourceGlass, 250);
  assert.equal(current.measures.sourceGlassMl, 250);
  assert.equal(current.sourceGlassPending, null);
  assert.equal(current.sourceGlassError, null);
});

scenario("only_the_newest_source_glass_answer_is_applied_whatever_answers_arrive", async () => {
  const { env } = await withMeasuredFavorite();
  const older = deferred();
  const newer = deferred();
  env.net.route("GET", FAV_URL_1, () => older.promise, () => newer.promise);

  const olderAsk = env.ctl.setSourceGlass(200);
  await flush();
  const newerAsk = env.ctl.setSourceGlass(250);
  await flush();
  let current = env.view().favorites.current;
  assert.equal(current.sourceGlass, null, "nothing is confirmed while the answers are out");
  assert.equal(current.sourceGlassPending, 250, "the newest choice is the pending one");

  // The answer for the older choice arrives first: it must not become the card, even for
  // a moment, and the newer answer must still be the one that is applied.
  older.resolve(H.favoriteDetail(chosenFavorite(convertedGlassBlock(200)), STRUCTURED_TEXT));
  await flush();
  current = env.view().favorites.current;
  assert.equal(current.sourceGlass, null);
  assert.equal(current.sourceGlassPending, 250);

  newer.resolve(H.favoriteDetail(chosenFavorite(convertedGlassBlock(250)), STRUCTURED_TEXT));
  await flush();
  current = env.view().favorites.current;
  assert.equal(current.sourceGlass, 250);
  assert.equal(current.measures.sourceGlassMl, 250);
  assert.equal(current.sourceGlassPending, null);

  assert.deepEqual(await olderAsk, { ok: false, reason: "stale" });
  assert.deepEqual(await newerAsk, { ok: true });
  await env.settle();
});

scenario("a_source_glass_answer_for_a_recipe_that_was_left_changes_nothing", async () => {
  const { env } = await withMeasuredFavorite();
  const second = H.structuredFavorite(H.FAV_2, 2, "Уха", 1, 0, {
    servings: null,
    ingredients: UKHA_RECIPE.ingredients,
    steps: UKHA_RECIPE.steps,
    version: 1,
    measures: measuresBlock({ source_glass_required: false, ingredients: [] }),
  });
  env.net.route("GET", FAV_URL_2, H.favoriteDetail(second, "Уха"));
  const answer = deferred();
  env.net.route("GET", FAV_URL_1, () => answer.promise);

  const asked = env.ctl.setSourceGlass(200);
  await flush();
  await env.ctl.openFavorite(H.FAV_2); // (the first recipe's read is still out)
  answer.resolve(H.favoriteDetail(chosenFavorite(convertedGlassBlock(200)), STRUCTURED_TEXT));
  await flush();

  const current = env.view().favorites.current;
  assert.equal(current.id, H.FAV_2);
  assert.equal(current.sourceGlass, null, "a choice belongs to the recipe it was made for");
  assert.equal(current.measures.sourceGlassMl, null);
  assert.deepEqual(await asked, { ok: false, reason: "stale" });
  await env.settle();
});

// --- KH-F01-B1 remediation: an edit keeps the confirmed clarification -------------------------

scenario("an_edited_title_keeps_the_confirmed_source_glass_conversion", async () => {
  const env = await withConfirmedGlass(200);
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, {
    title: "Борщ по-домашнему",
  });
  const updated = chosenFavorite(measuresBlock(), { title: draft.title, version: 4 });
  const refreshed = chosenFavorite(convertedGlassBlock(200), { title: draft.title, version: 4 });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, STRUCTURED_TEXT));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(refreshed, STRUCTURED_TEXT));

  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: true });
  await env.settle();

  // The edit itself was sent exactly as before, with no measure or clarification field.
  assert.deepEqual(env.net.requests("PATCH", FAV_URL_1)[0].body, {
    expected_version: 3,
    title: "Борщ по-домашнему",
    notes: "Больше укропа.",
    servings: "4 порции",
    ingredients: BORSH_RECIPE.ingredients,
    steps: BORSH_RECIPE.steps,
  });
  // The detail was read again with the confirmed choice, not with the edit's own block.
  assert.equal(
    env.net.requests("GET", FAV_URL_1).slice(-1)[0].url,
    FAV_URL_1 + "?source_glass_ml=200"
  );
  const current = env.view().favorites.current;
  assert.equal(current.title, "Борщ по-домашнему");
  assert.equal(current.version, 4);
  assert.equal(current.sourceGlass, 200);
  assert.equal(current.measures.sourceGlassMl, 200);
  assert.deepEqual(current.measures.ingredients[0].exact, { numerator: 4, denominator: 5 });
  assert.equal(current.measuresError, null);
  assert.equal(current.measuresPending, false);
});

scenario("an_edited_ingredient_amount_keeps_the_confirmed_source_glass_conversion", async () => {
  const env = await withConfirmedGlass(200);
  env.ctl.beginEditFavorite();
  const ingredients = [
    { name: "свекла", amount: "1 стакан" },
    { name: "капуста", amount: "300 г" },
  ];
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { ingredients });
  const updated = chosenFavorite(measuresBlock(), { ingredients, version: 4 });
  const refreshed = chosenFavorite(convertedGlassBlock(200), { ingredients, version: 4 });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, STRUCTURED_TEXT));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(refreshed, STRUCTURED_TEXT));

  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: true });
  await env.settle();

  assert.deepEqual(env.net.requests("PATCH", FAV_URL_1)[0].body.ingredients, ingredients);
  const current = env.view().favorites.current;
  assert.deepEqual(current.ingredients, ingredients);
  assert.equal(current.sourceGlass, 200);
  assert.equal(current.measures.sourceGlassMl, 200);
  assert.deepEqual(current.measures.ingredients[0].exact, { numerator: 4, denominator: 5 });
});

scenario("a_failed_post_save_conversion_read_shows_no_stale_hint_and_can_be_retried", async () => {
  const env = await withConfirmedGlass(200);
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, {
    title: "Борщ по-домашнему",
  });
  // The edit's answer was calculated without the confirmed clarification (the real PATCH
  // does not take one); it must not be drawn as this card's conversion.
  const updated = chosenFavorite(measuresBlock(), { title: draft.title, version: 4 });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, STRUCTURED_TEXT));
  env.net.route("GET", FAV_URL_1, fail());

  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: true }, "the edit itself is saved");
  await env.settle();

  let current = env.view().favorites.current;
  assert.equal(current.status, "ready");
  assert.equal(current.title, "Борщ по-домашнему");
  assert.equal(current.version, 4);
  assert.equal(current.measures, null, "no conversion is drawn from an unconfirmed context");
  assert.equal(current.measuresPending, false);
  assert.equal(current.measuresError.text, Logic.TEXT.measuresRefreshFailed);
  assert.equal(current.sourceGlass, 200, "the confirmed choice is kept for the retry");

  // "Повторить" reads the card again with that choice; no other hint is invented.
  const refreshed = chosenFavorite(convertedGlassBlock(200), { title: draft.title, version: 4 });
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(refreshed, STRUCTURED_TEXT));
  assert.equal(await env.ctl.retryFavoriteConversion(), true);
  await env.settle();

  current = env.view().favorites.current;
  assert.equal(current.title, "Борщ по-домашнему");
  assert.equal(current.measures.sourceGlassMl, 200);
  assert.deepEqual(current.measures.ingredients[0].exact, { numerator: 4, denominator: 5 });
  assert.equal(current.measuresPending, false);
  assert.equal(current.measuresError, null);
  assert.equal(
    env.net.requests("GET", FAV_URL_1).slice(-1)[0].url,
    FAV_URL_1 + "?source_glass_ml=200"
  );
});

scenario("a_post_save_conversion_answer_cannot_change_a_recipe_opened_meanwhile", async () => {
  const env = await withConfirmedGlass(200);
  const second = H.structuredFavorite(H.FAV_2, 2, "Уха", 1, 0, {
    servings: null,
    ingredients: UKHA_RECIPE.ingredients,
    steps: UKHA_RECIPE.steps,
    version: 1,
    measures: measuresBlock({ source_glass_required: false, ingredients: [] }),
  });
  env.net.route("GET", FAV_URL_2, H.favoriteDetail(second, "Уха"));
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, {
    title: "Борщ по-домашнему",
  });
  const updated = chosenFavorite(measuresBlock(), { title: draft.title, version: 4 });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, STRUCTURED_TEXT));
  const answer = deferred();
  env.net.route("GET", FAV_URL_1, () => answer.promise);

  const saving = env.ctl.saveFavoriteEdit(draft);
  await flush();
  await env.ctl.openFavorite(H.FAV_2); // the first recipe is left while its read is out
  answer.resolve(
    H.favoriteDetail(
      chosenFavorite(convertedGlassBlock(200), { title: draft.title, version: 4 }),
      STRUCTURED_TEXT
    )
  );
  await flush();

  const current = env.view().favorites.current;
  assert.equal(current.id, H.FAV_2);
  assert.equal(current.sourceGlass, null);
  assert.equal(current.measures.sourceGlassMl, null);
  assert.deepEqual(await saving, { ok: true }, "the edit itself was saved");
  await env.settle();
});

scenario("a_confirmed_source_glass_is_still_temporary_and_belongs_to_one_recipe", async () => {
  const env = await withConfirmedGlass(200);
  env.ctl.beginEditFavorite();
  const draft = Object.assign({}, env.view().favorites.current.editor.draft, {
    title: "Борщ по-домашнему",
  });
  const updated = chosenFavorite(measuresBlock(), { title: draft.title, version: 4 });
  const refreshed = chosenFavorite(convertedGlassBlock(200), { title: draft.title, version: 4 });
  env.net.route("PATCH", FAV_URL_1, H.favoriteDetail(updated, STRUCTURED_TEXT));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(refreshed, STRUCTURED_TEXT));
  await env.ctl.saveFavoriteEdit(draft);
  await env.settle();

  const second = H.structuredFavorite(H.FAV_2, 2, "Уха", 1, 0, {
    servings: null,
    ingredients: UKHA_RECIPE.ingredients,
    steps: UKHA_RECIPE.steps,
    version: 1,
    measures: measuresBlock({ source_glass_required: false, ingredients: [] }),
  });
  env.net.route("GET", FAV_URL_2, H.favoriteDetail(second, "Уха"));
  await env.ctl.openFavorite(H.FAV_2);
  await env.settle();

  const current = env.view().favorites.current;
  assert.equal(current.sourceGlass, null);
  assert.equal(current.measures.sourceGlassMl, null);
  assert.equal(env.net.requests("GET", FAV_URL_2).slice(-1)[0].url, FAV_URL_2);
  // Nothing was written to the profile, the recipe or the browser by the choice or the save.
  assert.equal(env.net.requests("PATCH", "/api/profile").length, 0);
});

// --- KH-F01-B1 remediation: a conversion block must agree with itself -------------------------

scenario("a_conversion_that_contradicts_its_own_status_is_ignored_whole", async () => {
  const contradictory = [
    {
      status: "unsupported",
      reason: "source_not_quantified",
      exact: { numerator: 99, denominator: 1 },
      rounded: null,
      identical: false,
      presentation: "exact",
    },
    {
      status: "ambiguous",
      reason: "source_not_quantified",
      exact: null,
      rounded: { numerator: 99, denominator: 1, relative_error: { numerator: 0, denominator: 1 } },
      identical: false,
      presentation: "rounded",
    },
    {
      status: "clarification_required",
      reason: "glass_size_required",
      exact: { numerator: 99, denominator: 1 },
      rounded: null,
      identical: false,
      presentation: "none",
    },
    {
      status: "unresolved",
      reason: "source_not_quantified",
      exact: null,
      rounded: null,
      identical: true,
      presentation: "none",
    },
  ];
  for (const item of contradictory) {
    const block = measuresBlock({
      ingredients: [{ position: 0, source: "1 стакан", source_kind: "household", target_unit: "glass", ...item }],
    });
    const { env } = await withMeasuredFavorite(block);
    const current = env.view().favorites.current;
    assert.equal(current.status, "ready", JSON.stringify(item));
    assert.equal(current.measures, null, JSON.stringify(item));
    assert.deepEqual(current.ingredients, BORSH_RECIPE.ingredients);
  }
});

scenario("a_successful_conversion_block_is_still_read_as_before", async () => {
  const env = await withConfirmedGlass(200);
  const current = env.view().favorites.current;
  assert.equal(current.status, "ready");
  assert.equal(current.measures.sourceGlassMl, 200);
  assert.deepEqual(current.measures.ingredients[0].exact, { numerator: 4, denominator: 5 });
  assert.equal(current.measures.ingredients[0].status, "nominal_household");
});

scenario("an_open_answer_whose_block_names_another_clarification_keeps_the_recipe_without_hints", async () => {
  // No ?source_glass_ml was sent, so a block that echoes 200 belongs to another context:
  // the recipe is drawn, its conversion is not.
  const { env } = await withMeasuredFavorite(convertedGlassBlock(200));
  const current = env.view().favorites.current;
  assert.equal(current.status, "ready");
  assert.equal(current.measures, null);
  assert.deepEqual(current.ingredients, BORSH_RECIPE.ingredients);
});

// --- KH-F01-B1 remediation 02: an editor keeps its own baseline version ---------------------

scenario("an_editor_keeps_its_base_version_when_a_pending_source_glass_answer_arrives", async () => {
  const env = await withConfirmedGlass(200);
  const pending = deferred();
  env.net.route("GET", FAV_URL_1, () => pending.promise);

  const asked = env.ctl.setSourceGlass(250);
  await flush();
  assert.equal(env.view().favorites.current.sourceGlassPending, 250);

  assert.equal(env.ctl.beginEditFavorite(), true);
  let current = env.view().favorites.current;
  assert.equal(current.version, 3);
  assert.equal(current.editor.baseVersion, 3);
  assert.equal(current.sourceGlassPending, null, "the pending read is abandoned with the editor's baseline");
  const draftBefore = JSON.parse(JSON.stringify(current.editor.draft));

  // The answer that was out carries a version another device made meanwhile.
  pending.resolve(
    H.favoriteDetail(
      chosenFavorite(convertedGlassBlock(250), {
        title: "Борщ, изменённый в другом месте",
        ingredients: [{ name: "свекла", amount: "9 шт." }],
        version: 4,
      }),
      STRUCTURED_TEXT
    )
  );
  await flush();

  current = env.view().favorites.current;
  assert.deepEqual(await asked, { ok: false, reason: "stale" });
  assert.equal(current.version, 3, "the card under the draft was not replaced");
  assert.equal(current.title, "Борщ");
  assert.equal(current.sourceGlass, 200, "the confirmed choice is untouched");
  assert.equal(current.measures.sourceGlassMl, 200, "its block is still the 200 mL one");
  assert.deepEqual(current.measures.ingredients[0].exact, { numerator: 4, denominator: 5 });
  assert.deepEqual(current.editor.draft, draftBefore, "the draft is exactly as it was opened");
  assert.equal(current.editor.baseVersion, 3);

  // Cancelling gives the card back exactly as the draft was based on, still consistent.
  assert.equal(env.ctl.cancelEditFavorite(), true);
  current = env.view().favorites.current;
  assert.equal(current.editor, null);
  assert.equal(current.version, 3);
  assert.equal(current.sourceGlass, 200);
  assert.equal(current.measures.sourceGlassMl, 200);
  assert.deepEqual(current.measures.ingredients[0].exact, { numerator: 4, denominator: 5 });
});

scenario("a_stale_draft_is_saved_with_the_version_it_started_from_and_the_conflict_keeps_it", async () => {
  const env = await withConfirmedGlass(200);
  const pending = deferred();
  env.net.route("GET", FAV_URL_1, () => pending.promise);
  const asked = env.ctl.setSourceGlass(250);
  await flush();
  assert.equal(env.ctl.beginEditFavorite(), true);
  pending.resolve(
    H.favoriteDetail(
      chosenFavorite(convertedGlassBlock(250), {
        title: "Борщ, изменённый в другом месте",
        ingredients: [{ name: "свекла", amount: "9 шт." }],
        version: 4,
      }),
      STRUCTURED_TEXT
    )
  );
  await flush();
  assert.deepEqual(await asked, { ok: false, reason: "stale" });

  const draft = Object.assign({}, env.view().favorites.current.editor.draft, { title: "Мой борщ" });
  env.net.route("PATCH", FAV_URL_1, apiError(409, "favorite_conflict"));
  assert.deepEqual(await env.ctl.saveFavoriteEdit(draft), { ok: false, reason: "conflict" });

  const sent = env.net.requests("PATCH", FAV_URL_1)[0].body;
  assert.equal(sent.expected_version, 3, "the draft names the version it was opened on, never a later one");
  assert.equal(sent.title, "Мой борщ");

  let current = env.view().favorites.current;
  assert.equal(current.editor.phase, "conflict");
  assert.deepEqual(current.editor.draft, draft, "the unsaved draft is kept");
  assert.equal(current.editor.error.text, Logic.TEXT.editConflict);

  // A reload finds the other device's change, intact, and no draft of ours is written.
  const changed = chosenFavorite(convertedGlassBlock(200), {
    title: "Борщ, изменённый в другом месте",
    ingredients: [{ name: "свекла", amount: "9 шт." }],
    version: 4,
  });
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(changed, STRUCTURED_TEXT));
  await env.ctl.reloadFavoriteAfterConflict();
  await env.settle();

  current = env.view().favorites.current;
  assert.equal(current.editor, null);
  assert.equal(current.version, 4);
  assert.equal(current.title, "Борщ, изменённый в другом месте");
  assert.deepEqual(current.ingredients, [{ name: "свекла", amount: "9 шт." }]);
  assert.equal(env.net.requests("PATCH", FAV_URL_1).length, 1, "the stale draft was never stored");
});

scenario("a_source_glass_choice_cannot_be_started_while_an_editor_is_open", async () => {
  const env = await withConfirmedGlass(200);
  env.ctl.beginEditFavorite();
  const asked = env.net.log.length;

  assert.deepEqual(await env.ctl.setSourceGlass(250), { ok: false, reason: "editing" });
  assert.deepEqual(await env.ctl.setSourceGlass("300"), { ok: false, reason: "editing" });
  assert.equal(env.net.log.length, asked, "no read is started over an open draft");

  let current = env.view().favorites.current;
  assert.equal(current.sourceGlass, 200);
  assert.equal(current.sourceGlassPending, null);
  assert.equal(current.editor.phase, "editing");

  // Once the draft is given up, the question may be asked again.
  env.ctl.cancelEditFavorite();
  const second = chosenFavorite(convertedGlassBlock(250));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(second, STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.setSourceGlass(250), { ok: true });
  current = env.view().favorites.current;
  assert.equal(current.sourceGlass, 250);
  assert.equal(current.measures.sourceGlassMl, 250);
});

// --- KH-F01-B1 remediation 02: the custom capacity is a draft, not a choice -----------------

scenario("a_custom_capacity_is_a_draft_of_its_own_until_the_server_confirms_it", async () => {
  const { env } = await withMeasuredFavorite();
  const asked = env.net.log.length;

  assert.deepEqual(env.ctl.toggleSourceGlassOther(), { open: true, changed: true });
  let current = env.view().favorites.current;
  assert.equal(current.sourceGlassOtherOpen, true);
  assert.equal(current.sourceGlass, null, "opening the field confirms nothing");
  assert.equal(current.sourceGlassDraft, "");
  assert.equal(current.measures.sourceGlassMl, null);

  assert.equal(env.ctl.syncSourceGlassDraft("300"), true);
  current = env.view().favorites.current;
  assert.equal(current.sourceGlassDraft, "300");
  assert.equal(current.sourceGlass, null, "typing confirms nothing");
  assert.equal(current.sourceGlassPending, null);
  assert.equal(current.measures.sourceGlassMl, null);
  assert.equal(env.net.log.length, asked, "a draft sends nothing");

  // Any redraw (a session recheck, another control) rebuilds the field from the draft.
  assert.deepEqual(env.ctl.toggleSourceGlassOther(), { open: false, changed: true });
  assert.deepEqual(env.ctl.toggleSourceGlassOther(), { open: true, changed: true });
  current = env.view().favorites.current;
  assert.equal(current.sourceGlassDraft, "300");
  assert.equal(current.sourceGlass, null);

  // Only the answer for the typed value makes it the confirmed choice.
  const chosen = chosenFavorite(convertedGlassBlock(300));
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(chosen, STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.setSourceGlass("300"), { ok: true });
  await env.settle();
  assert.equal(
    env.net.requests("GET", FAV_URL_1).slice(-1)[0].url,
    FAV_URL_1 + "?source_glass_ml=300"
  );
  current = env.view().favorites.current;
  assert.equal(current.sourceGlass, 300);
  assert.equal(current.measures.sourceGlassMl, 300);
  assert.equal(current.sourceGlassOtherOpen, true);
  assert.equal(current.sourceGlassDraft, "300", "the field holds the confirmed capacity");

  // Closing and reopening the disclosure shows that capacity again.
  assert.deepEqual(env.ctl.toggleSourceGlassOther(), { open: false, changed: true });
  assert.deepEqual(env.ctl.toggleSourceGlassOther(), { open: true, changed: true });
  assert.equal(env.view().favorites.current.sourceGlassDraft, "300");

  // A standard capacity is confirmed by its own answer too, and closes the field; the
  // typed text stays a draft and confirms nothing.
  env.net.route("GET", FAV_URL_1, H.favoriteDetail(chosenFavorite(convertedGlassBlock(200)), STRUCTURED_TEXT));
  assert.deepEqual(await env.ctl.setSourceGlass(200), { ok: true });
  current = env.view().favorites.current;
  assert.equal(current.sourceGlass, 200);
  assert.equal(current.measures.sourceGlassMl, 200);
  assert.equal(current.sourceGlassOtherOpen, false);
  assert.equal(current.sourceGlassDraft, "300");
  env.ctl.toggleSourceGlassOther();
  current = env.view().favorites.current;
  assert.equal(current.sourceGlass, 200, "the draft still confirms nothing beside it");
  assert.equal(current.measures.sourceGlassMl, 200);
  assert.equal(current.sourceGlassDraft, "300");

  // A different recipe starts without a confirmed capacity, without a draft and closed.
  const second = H.structuredFavorite(H.FAV_2, 2, "Уха", 1, 0, {
    servings: null,
    ingredients: UKHA_RECIPE.ingredients,
    steps: UKHA_RECIPE.steps,
    version: 1,
    measures: measuresBlock({ source_glass_required: false, ingredients: [] }),
  });
  env.net.route("GET", FAV_URL_2, H.favoriteDetail(second, "Уха"));
  await env.ctl.openFavorite(H.FAV_2);
  await env.settle();
  current = env.view().favorites.current;
  assert.equal(current.sourceGlass, null);
  assert.equal(current.sourceGlassOtherOpen, false);
  assert.equal(current.sourceGlassDraft, "");
});

scenario("a_failed_first_custom_volume_leaves_the_capacity_unconfirmed", async () => {
  const { env } = await withMeasuredFavorite();
  assert.deepEqual(env.ctl.toggleSourceGlassOther(), { open: true, changed: true });
  env.ctl.syncSourceGlassDraft("300");
  env.net.route("GET", FAV_URL_1, fail());

  assert.deepEqual(await env.ctl.setSourceGlass("300"), { ok: false, reason: "failed" });
  await env.settle();

  // There was no confirmed capacity before, and there is none now: the draft and the
  // failure are the only things the card gained.
  const current = env.view().favorites.current;
  assert.equal(current.sourceGlass, null);
  assert.equal(current.measures.sourceGlassMl, null);
  assert.equal(current.sourceGlassOtherOpen, true);
  assert.equal(current.sourceGlassDraft, "300");
  assert.equal(current.sourceGlassError.value, 300);
  assert.equal(current.sourceGlassError.text, Logic.TEXT.sourceGlassFailed);
  assert.equal(current.sourceGlassPending, null);
});

module.exports = scenarios;
