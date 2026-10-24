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
  assert.equal(view.list.items.find((i) => i.id === CONV_A).meta, "ждёт решения");

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
  // Placeholders are filled in; "JSON" is the one word the server's own catalogue writes in Latin.
  const stripPlaceholders = (value) => value.replace(/\{\w+\}/g, "").replace(/JSON/g, "");
  for (const [name, value] of Object.entries({ ...Logic.TEXT, ...Logic.CODE_MESSAGES })) {
    const shown = stripPlaceholders(value);
    assert.ok(/[А-Яа-яЁё]/.test(shown), `${name} has no Russian`);
    assert.ok(!latin.test(shown), `${name} has Latin letters: ${shown}`);
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

const SENT = json(202, { detail: "link_requested", message: "Если этот адрес есть в списке разрешённых, мы отправили письмо." });

scenario("the_email_address_is_trimmed_and_nothing_else_the_local_part_keeps_its_case", async () => {
  const env = makeEnv();
  env.net.route("POST", "/auth/email/request", SENT);
  const result = await env.ctl.requestEmailLink("  Cook@Example.COM \n");

  assert.equal(result.ok, true);
  const request = env.net.log[0];
  assert.deepEqual(request.body, { email: "Cook@Example.COM" });
  assert.equal(request.headers["content-type"], "application/json");
  assert.equal(request.init.credentials, "same-origin");
  assert.equal(result.text, "Если этот адрес есть в списке разрешённых, мы отправили письмо.");
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
    json(403, { detail: "forbidden_origin", message: "Запрос отклонён: он пришёл не с нашего сайта." }),
    json(503, { detail: "login_unavailable" }),
    json(503, { detail: "temporarily_unavailable", message: "Сервис временно недоступен. Попробуйте ещё раз чуть позже." }),
    json(500, { detail: "server_error", message: "Что-то пошло не так. Попробуйте ещё раз чуть позже." }),
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
    "GET /auth/me", "GET /auth/me", "GET /api/conversations", `GET /api/conversations/${CONV_B}/messages`,
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

module.exports = scenarios;
