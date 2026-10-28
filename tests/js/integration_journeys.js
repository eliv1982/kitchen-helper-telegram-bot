"use strict";
/*
 * chat-logic.js against a REAL server (the application on a loopback port, started by
 * tests/test_web_ui_integration.py), over real HTTP, with a fake assistant. A small fetch
 * wrapper stands in for the browser's part: the session cookie, the Origin a browser puts on a
 * POST, and Sec-Fetch-Site. It can also lose a request or an answer, the way a network does.
 *
 * Usage: node integration_journeys.js <name> <base url> <cookie header> [<another person's cookie header>]
 * The browser has one cookie jar, shared by its tabs (pages): a journey that has another person sign in, in
 * another tab, puts their cookie in the jar. Prints {"ok": true, "data": ...} or {"ok": false, "error": ...} as one JSON line.
 *
 * Timers are the real ones, but scaled down (a 5 s Retry-After is waited for 250 ms), so the
 * journeys are quick; the page's own timeouts shrink with them, which is why the assistant here
 * is never slower than about a second.
 */

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const H = require("./harness.js");

const { Logic, Storage } = H;
const SCALE = 0.2;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(condition, label, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs;
  for (;;) {
    const value = await condition();
    if (value) return value;
    if (Date.now() > end) throw new Error("timed out waiting for " + label);
    await sleep(15);
  }
}

// --- the browser's part ---------------------------------------------------------------------------------

function browser(base, jar) {
  const log = [];
  const faults = [];
  const real = (url, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    const headers = { ...(init.headers || {}), Cookie: jar.cookie, "Sec-Fetch-Site": "same-origin" };
    if (method !== "GET") headers.Origin = base; // a browser sends it with every POST
    return fetch(base + url, { method, headers, body: init.body, signal: init.signal, redirect: init.redirect });
  };
  const wrapped = async (url, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    const entry = { method, url, key: (init.headers || {})["Idempotency-Key"] || null, status: null, setCookie: null };
    log.push(entry);
    const index = faults.findIndex((fault) => fault.match(method, url));
    if (index >= 0) {
      const [fault] = faults.splice(index, 1);
      return fault.act(real, url, init, entry);
    }
    const response = await real(url, init);
    entry.status = response.status;
    entry.setCookie = response.headers.get("set-cookie");
    return response;
  };
  return {
    fetch: wrapped,
    real,
    log,
    // The next request that matches is lost before it reaches the server...
    dropRequest(match) {
      faults.push({ match, act: async () => { throw new TypeError("fetch failed (request lost)"); } });
    },
    // ...or is processed by the server and its answer is lost on the way back.
    loseAnswer(match) {
      faults.push({
        match,
        act: async (real, url, init, entry) => {
          const response = await real(url, init);
          entry.status = response.status;
          entry.setCookie = response.headers.get("set-cookie");
          await response.text();
          throw new TypeError("fetch failed (answer lost)");
        },
      });
    },
    // A raw request, outside the page's logic, as another client or a script would make it.
    async raw(method, url, body, key, extra = {}) {
      const headers = { "Content-Type": "application/json", ...extra };
      if (key) headers["Idempotency-Key"] = key;
      const response = await real(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await response.text();
      let json = null;
      try { json = JSON.parse(text); } catch (ignored) { /* not JSON */ }
      return { status: response.status, json, headers: response.headers };
    },
  };
}

function page(base, cookie, { storage = new Storage() } = {}) {
  const net = browser(base, typeof cookie === "string" ? { cookie } : cookie); // (a string: that person's own browser; a jar: shared)
  const ctl = Logic.createController({
    fetch: net.fetch,
    storage,
    crypto: crypto.webcrypto,
    setTimeout: (fn, ms) => setTimeout(fn, Math.max(5, Math.round(ms * SCALE))),
    clearTimeout,
    now: () => Date.now(),
    AbortController,
  });
  const view = () => ctl.getView();
  return {
    ctl, net, storage, view,
    items: () => view().thread.items,
    texts: () => view().thread.items.map((item) => item.text),
    pending: () => view().thread.items.find((item) => item.type === "pending"),
    async boot() {
      await ctl.start();
      await ctl.idle();
      return view();
    },
    posts: (pattern = /messages$/) => net.log.filter((r) => r.method === "POST" && pattern.test(r.url)),
  };
}

const journeys = {};

// --- journeys -----------------------------------------------------------------------------------------------

journeys.first_conversation = async ({ base, cookie }) => {
  const p = page(base, cookie);
  let view = await p.boot();
  assert.equal(view.screen, "app");
  assert.equal(view.currentId, null, "nothing yet: a draft");
  assert.equal(view.list.items.length, 0);

  const sent = await p.ctl.send("Как варить борщ?");
  assert.equal(sent.ok, true);
  await sent.done;
  view = p.view();

  assert.deepEqual(p.texts(), ["Как варить борщ?", "Ответ на: Как варить борщ?"]);
  assert.equal(view.list.items.length, 1);
  assert.equal(view.list.items[0].label, "Как варить борщ?", "named by its first message");
  assert.equal(view.currentId, view.list.items[0].id);
  assert.deepEqual(p.net.log.map((r) => `${r.method} ${r.url.replace(/[0-9a-f-]{36}/, "<id>")}`), [
    "GET /auth/me", "GET /api/conversations?limit=50", "GET /api/favorites", "POST /api/conversations",
    "POST /api/conversations/<id>/messages",
  ]);
  assert.ok(p.net.log.every((r) => r.status === null || r.status < 300), JSON.stringify(p.net.log));
  return { conversation: view.currentId };
};

journeys.reload_restores_the_history_and_the_list = async ({ base, cookie }) => {
  const first = page(base, cookie);
  await first.boot();
  await (await first.ctl.send("Первый вопрос")).done;
  await (await first.ctl.send("Второй вопрос")).done;

  const reloaded = page(base, cookie); // a new tab: nothing in memory, nothing in storage
  const view = await reloaded.boot();

  assert.equal(view.currentId, first.view().currentId);
  assert.deepEqual(reloaded.texts(), ["Первый вопрос", "Ответ на: Первый вопрос", "Второй вопрос", "Ответ на: Второй вопрос"]);
  assert.equal(view.list.items.length, 1);
  assert.equal(view.list.items[0].label, "Первый вопрос", "the server names it by its first message, not the second");
  assert.equal(first.view().list.items[0].label, "Первый вопрос");
  assert.ok(reloaded.items().every((item) => item.time && item.iso), "every message has a time");
  assert.equal(reloaded.posts().length, 0, "reading sends nothing");
  assert.equal(view.composer.locked, false);
};

journeys.paging_through_a_long_history = async ({ base, cookie }) => {
  const first = page(base, cookie);
  await first.boot();
  for (let i = 1; i <= 30; i++) {
    await (await first.ctl.send("вопрос " + i)).done;
  }

  const second = page(base, cookie);
  let view = await second.boot();
  assert.equal(second.items().length, 50);
  assert.equal(view.thread.hasMore, true);
  assert.equal(second.items()[0].text, "вопрос 6");

  await second.ctl.loadOlder();
  await until(() => !second.view().thread.loadingOlder, "older page");
  const items = second.items();
  assert.equal(items.length, 60);
  assert.equal(second.view().thread.hasMore, false);
  const ids = items.map((item) => item.id);
  assert.deepEqual(ids, [...ids].sort((a, b) => a - b));
  assert.equal(new Set(ids).size, 60);
  assert.deepEqual(items.filter((i) => i.role === "user").map((i) => i.text), Array.from({ length: 30 }, (_, k) => "вопрос " + (k + 1)));
  assert.ok(second.net.log.some((r) => /before=\d+/.test(r.url)));
};

journeys.a_lost_answer_is_seen_in_the_history_and_confirmed_by_the_same_key_without_a_second_run = async ({ base, cookie }) => {
  const p = page(base, cookie);
  await p.boot();
  p.net.loseAnswer((method, url) => method === "POST" && /messages$/.test(url));
  const sent = await p.ctl.send("потерянный ответ");
  await sent.done;
  await until(() => p.pending() && p.pending().note === Logic.TEXT.uncertainSeen, "the exchange to be seen in the history");

  // Seen, not settled: the exchange is in the history once, and the message waits for its own key.
  const messages = () => p.items().filter((i) => i.type === "message").map((i) => i.text);
  assert.deepEqual(messages(), ["потерянный ответ", "Ответ на: потерянный ответ"]);
  assert.deepEqual(p.pending().actions, ["retry"]);
  assert.equal(p.posts().length, 1, "nothing was sent again");
  assert.equal(p.view().composer.locked, true);

  await p.ctl.retry(p.view().currentId);
  await until(() => !p.pending(), "the replay to settle it");
  const posts = p.posts();
  assert.deepEqual(posts.map((r) => r.status), [201, 200], "the second is the server's replay of the first");
  assert.equal(posts[0].key, posts[1].key);
  assert.deepEqual(p.texts(), ["потерянный ответ", "Ответ на: потерянный ответ"]);
  assert.equal(p.storage.record, null);
  assert.equal(p.view().composer.locked, false);
  const history = await p.net.raw("GET", `/api/conversations/${p.view().currentId}/messages`);
  assert.equal(history.json.messages.length, 2);
};

journeys.a_lost_request_is_retried_with_the_same_key_and_runs_once = async ({ base, cookie }) => {
  const p = page(base, cookie);
  await p.boot();
  p.net.dropRequest((method, url) => method === "POST" && /messages$/.test(url));
  const sent = await p.ctl.send("потерянный запрос");
  await sent.done;
  await until(() => p.pending() && !p.view().thread.items.some((i) => i.note === Logic.TEXT.checking), "uncertain");

  assert.equal(p.pending().phase, "uncertain");
  assert.deepEqual(p.pending().actions, ["retry"]);
  await p.ctl.retry(p.view().currentId);
  await until(() => !p.pending(), "retry to resolve");

  const posts = p.posts();
  assert.equal(posts.length, 2);
  assert.equal(posts[0].key, posts[1].key);
  assert.deepEqual([posts[0].status, posts[1].status], [null, 201]);
  assert.deepEqual(p.texts(), ["потерянный запрос", "Ответ на: потерянный запрос"]);
  return { key: posts[0].key };
};

journeys.a_replay_after_an_unseen_completion_is_one_answer_not_two = async ({ base, cookie }) => {
  const p = page(base, cookie);
  await p.boot();
  const isMessages = (method, url) => method === "POST" && /messages$/.test(url);
  p.net.loseAnswer(isMessages);
  // ...and the page cannot read the history afterwards either (the network is down):
  p.net.dropRequest((method, url) => method === "GET" && /\/messages\?limit=50$/.test(url));
  const sent = await p.ctl.send("нет связи после ответа");
  await sent.done;
  await until(() => p.pending() && p.pending().phase === "uncertain" && !p.pending().busy, "uncertain, unchecked");
  assert.deepEqual(p.texts(), ["нет связи после ответа"], "only the unresolved message is shown");

  await p.ctl.retry(p.view().currentId); // the server replays: the same key, the same text
  await until(() => !p.pending(), "replay");
  const posts = p.posts();
  assert.deepEqual(posts.map((r) => r.status), [201, 200]);
  assert.equal(posts[0].key, posts[1].key);
  assert.deepEqual(p.texts(), ["нет связи после ответа", "Ответ на: нет связи после ответа"]);
};

journeys.a_busy_conversation_waits_and_then_sends_the_same_key = async ({ base, cookie }) => {
  const one = page(base, cookie);
  await one.boot();
  await (await one.ctl.send("начало")).done;
  const conversation = one.view().currentId;
  const slow = await one.ctl.send("slow первый"); // the assistant takes most of a second
  await until(async () => {
    const h = await one.net.raw("GET", `/api/conversations/${conversation}/messages`);
    return h.json.messages.some((m) => m.status === "in_progress");
  }, "the slow turn to be in progress");

  const two = page(base, cookie); // another tab
  const view = await two.boot();
  assert.equal(view.currentId, conversation);
  assert.equal(view.composer.locked, true, "the page sees the reply being prepared and waits");
  const second = await two.ctl.send("второй вопрос");
  await until(() => two.pending() && two.pending().phase === "waiting", "waiting");
  assert.equal(two.pending().note, Logic.TEXT.waitingBusy);

  await second.done;
  await slow.done;
  await until(() => !two.pending() && two.texts().includes("Ответ на: второй вопрос"), "the second answer");
  const posts = two.posts();
  assert.ok(posts.length >= 2, "it asked again");
  assert.equal(new Set(posts.map((r) => r.key)).size, 1, "always the same key");
  assert.equal(posts[0].status, 409);
  assert.equal(posts[posts.length - 1].status, 201);
};

// The application's own 502 is the answer to a request, not a verdict: the page asks the history, and only
// when the history has the message "failed" under its key is it known to have failed (and it is the history's).
journeys.a_provider_failure_is_known_only_through_the_history_and_the_same_key_tries_again = async ({ base, cookie }) => {
  const p = page(base, cookie);
  await p.boot();
  const sent = await p.ctl.send("break once пожалуйста");
  await sent.done;
  await until(() => !p.pending() && p.items().some((i) => i.status === "failed"), "the history to say it failed");

  const failed = p.items().find((i) => i.status === "failed");
  assert.equal(failed.note, Logic.CODE_MESSAGES.provider_unreachable);
  assert.match(failed.correlationId, /^hh-[0-9a-f]{16}$/, "the failed request's number stays with the message");
  assert.equal(failed.canRetry, true);
  assert.equal(p.storage.record, null, "handed over to the history");
  await p.ctl.retryMessage(p.view().currentId);
  await until(() => !p.items().some((i) => i.status === "failed") && p.texts().includes("Ответ на: break once пожалуйста"), "retry");

  const posts = p.posts();
  assert.deepEqual(posts.map((r) => r.status), [502, 201]);
  assert.equal(posts[0].key, posts[1].key);
  assert.deepEqual(p.texts(), ["break once пожалуйста", "Ответ на: break once пожалуйста"]);
};

journeys.a_message_passed_over_can_only_be_sent_again_as_a_new_one = async ({ base, cookie }) => {
  const one = page(base, cookie);
  await one.boot();
  await (await one.ctl.send("break always первый")).done;
  await until(() => !one.pending() && one.items().some((i) => i.status === "failed" && i.canRetry), "failure");
  const conversation = one.view().currentId;
  const firstKey = one.posts()[0].key;

  const two = page(base, cookie); // the person went on in another tab
  await two.boot();
  assert.equal(two.view().currentId, conversation);
  await (await two.ctl.send("дальше без сбоя")).done;
  assert.ok(two.texts().includes("Ответ на: дальше без сбоя"));

  await one.ctl.retryMessage(conversation); // (this tab still shows it as the last, and failed)
  await until(() => one.pending() && one.pending().phase === "rejected", "rejection");
  assert.deepEqual(one.pending().actions, ["asnew", "edit"]);
  assert.equal(one.pending().note, Logic.CODE_MESSAGES.turn_superseded);
  assert.equal(one.posts().length, 2, "nothing more was sent by itself");

  await one.ctl.sendAsNew(conversation);
  await until(() => one.posts().length === 3 && !one.pending() && one.items().some((i) => i.status === "failed"), "the new attempt");
  const keys = one.posts().map((r) => r.key);
  assert.equal(keys[0], keys[1]);
  assert.notEqual(keys[2], firstKey, "a fresh key, because the person chose to send it as new");
};

// The server completes and STORES the turn, and the answer is the application's own 503 (a lost commit
// acknowledgement; tests/test_web_ui_integration.py makes the live server do exactly that for a message that
// starts with "ack lost"). The page used to call that a failure and offer the text back / a new message.
journeys.a_503_after_a_committed_completion_is_one_turn_whatever_is_tried = async ({ base, cookie }) => {
  const p = page(base, cookie);
  await p.boot();
  const sent = await p.ctl.send("ack lost вопрос");
  await sent.done;
  await until(() => p.pending() && p.pending().note === Logic.TEXT.uncertainSeen, "the stored exchange to be seen in the history");

  const conversation = p.view().currentId;
  assert.deepEqual([p.pending().phase, p.pending().actions], ["uncertain", ["retry"]]);
  assert.match(p.pending().correlationId, /^hh-[0-9a-f]{16}$/, "the 503's number is kept");
  assert.deepEqual(p.posts().map((r) => r.status), [503]);
  assert.equal(p.ctl.discard(conversation), null, "the text cannot be taken back");
  await p.ctl.sendAsNew(conversation);
  const again = await p.ctl.send("ack lost вопрос");
  assert.deepEqual([again.ok, again.reason], [false, "busy"]);
  assert.equal(p.posts().length, 1, "nothing else was sent");
  assert.equal(p.view().composer.locked, true);

  await p.ctl.retry(conversation);
  await until(() => !p.pending(), "the replay");
  const posts = p.posts();
  assert.deepEqual(posts.map((r) => r.status), [503, 200], "the second is the server's replay of the stored turn");
  assert.equal(posts[0].key, posts[1].key);
  assert.deepEqual(p.texts(), ["ack lost вопрос", "Ответ на: ack lost вопрос"]);
  assert.equal(p.storage.record, null);
  const history = await p.net.raw("GET", `/api/conversations/${conversation}/messages`);
  assert.equal(history.json.messages.length, 2, "one logical turn on the server");
};

journeys.the_limit_is_in_code_points_end_to_end = async ({ base, cookie }) => {
  const p = page(base, cookie);
  await p.boot();
  const emoji = "😀".repeat(2000);
  const sent = await p.ctl.send(emoji);
  assert.equal(sent.ok, true);
  await sent.done;
  const conversation = p.view().currentId;
  assert.equal(p.items()[0].text, emoji);

  const refused = await p.ctl.send("😀".repeat(2001));
  assert.deepEqual([refused.ok, refused.reason], [false, "too_long"]);
  assert.equal(p.posts().length, 1, "the page asked nothing");

  // The server decides in the end: the same text sent around the page is refused too.
  const raw = await p.net.raw("POST", `/api/conversations/${conversation}/messages`, { text: "😀".repeat(2001) }, "web-raw-too-long");
  assert.equal(raw.status, 400);
  assert.equal(raw.json.detail, "message_too_long");

  const reloaded = page(base, cookie);
  await reloaded.boot();
  assert.equal(reloaded.items()[0].text, emoji, "stored and read back exactly");
};

journeys.the_session_ending_is_noticed_and_the_unsent_message_is_kept_for_next_time = async ({ base, cookie }) => {
  const storage = new Storage();
  const p = page(base, cookie, { storage });
  await p.boot();
  await (await p.ctl.send("пока вхожу")).done;
  const conversation = p.view().currentId;

  const who = (await p.net.raw("GET", "/auth/me")).json.user_id;
  const out = await p.net.raw("POST", "/auth/logout", undefined, undefined, { "X-Expected-User": who });
  assert.equal(out.status, 204, "signed out somewhere else");
  const sent = await p.ctl.send("уже вышел");
  await sent.done;
  // (If the connection is reset before the 401 is read, the page reads the history and finds out
  // there: the same screen a moment later.)
  await until(() => p.view().screen === "login", "the login screen");

  const view = p.view();
  assert.equal(view.screen, "login");
  assert.equal(view.notice.text, Logic.TEXT.authExpired);
  assert.equal(view.list.items.length, 0);
  assert.deepEqual(Object.keys(p.ctl._state().threads), []);
  assert.equal(storage.record.turns.length, 1);
  assert.equal(storage.record.turns[0].t, "уже вышел");
  assert.equal(storage.record.turns[0].c, conversation);

  await p.ctl.start();
  await p.ctl.idle();
  assert.equal(p.view().screen, "login", "still signed out: a definite 401, not 'unavailable'");
};

// --- signing out ------------------------------------------------------------------------------------------------
// The sign-out says whose it is (X-Expected-User), so that the server ends that person's session or none: the answer
// to it can be lost, and by the time the page asks again the browser's one cookie may be somebody else's.

const isLogout = (method, url) => method === "POST" && url === "/auth/logout";
const logouts = (p) => p.net.log.filter((r) => isLogout(r.method, r.url));

journeys.a_lost_answer_to_sign_out_cannot_end_the_session_of_whoever_signed_in_since = async ({ base, cookie, other }) => {
  const jar = { cookie }; // the browser's one cookie jar
  const anna = page(base, jar);
  await anna.boot();
  await (await anna.ctl.send("секрет Анны")).done;
  assert.deepEqual(anna.texts(), ["секрет Анны", "Ответ на: секрет Анны"]);
  const annaId = (await anna.net.raw("GET", "/auth/me")).json.user_id;

  // The server ends her session; the answer is lost on the way back, so the browser keeps her (dead) cookie.
  anna.net.loseAnswer(isLogout);
  assert.equal(await anna.ctl.logout(), false);
  assert.equal(logouts(anna)[0].status, 204, "the server did end it");
  assert.deepEqual([anna.view().screen, anna.view().failure.retry], ["unavailable", "logout"]);
  assert.equal((await anna.net.raw("GET", "/auth/me")).status, 401, "her session is over");

  // Boris signs in, in another tab of the same browser: the cookie is his now.
  jar.cookie = other;
  const boris = page(base, jar);
  await boris.boot();
  assert.equal(boris.view().screen, "app");
  const me = await boris.net.raw("GET", "/auth/me");
  const borisId = me.json.user_id;
  assert.equal(me.status, 200);
  assert.notEqual(borisId, annaId);

  // Her old page offers to leave again. The server sees his cookie and her name, and ends nothing.
  assert.equal(await anna.ctl.recover(), false);
  const retry = logouts(anna)[1];
  assert.equal(retry.status, 409);
  assert.equal(retry.setCookie, null, "his cookie is not cleared");
  const view = anna.view();
  assert.deepEqual([view.screen, view.failure.text, view.failure.retry], ["unavailable", Logic.TEXT.logoutMismatch, "start"]);
  assert.equal(anna.ctl._logoutPrincipal(), null);
  const traces = () => JSON.stringify([anna.view(), anna.storage.data, anna.ctl._state()]);
  assert.ok(!traces().includes("секрет Анны") && !traces().includes(annaId), "nothing of hers is kept, not even who she was");

  // He is still signed in and the sign-out was not repeated by itself.
  const still = await boris.net.raw("GET", "/auth/me");
  assert.deepEqual([still.status, still.json.user_id], [200, borisId]);
  assert.equal((await boris.net.raw("GET", "/api/conversations")).status, 200);
  await anna.ctl.recheck();
  await sleep(100);
  assert.equal(logouts(anna).length, 2);

  // The button of her page begins again from "who is signed in": his page, with none of hers.
  await anna.ctl.recover();
  await anna.ctl.idle();
  assert.equal(anna.view().screen, "app");
  assert.deepEqual(anna.texts(), []);
  assert.ok(!traces().includes("секрет Анны"));
  assert.equal(logouts(anna).length, 2, "nobody was signed out");
  return { annaId, borisId };
};

journeys.a_sign_out_whose_answer_was_lost_is_repeated_by_the_same_person_and_closes_nothing_twice = async ({ base, cookie }) => {
  const jar = { cookie };
  const p = page(base, jar);
  await p.boot();
  await (await p.ctl.send("пока выхожу")).done;

  p.net.loseAnswer(isLogout);
  assert.equal(await p.ctl.logout(), false);
  assert.equal((await p.net.raw("GET", "/auth/me")).status, 401, "the server ended the session");

  // The cookie is still hers (the answer that would have cleared it never came): she repeats.
  assert.equal(await p.ctl.recover(), true);
  const [first, second] = logouts(p);
  assert.deepEqual([first.status, second.status], [204, 204]);
  assert.match(second.setCookie, /Max-Age=0/i, "the repeat clears the dead cookie");
  assert.deepEqual([p.view().screen, p.view().notice.text], ["login", Logic.TEXT.loggedOut]);
  assert.equal(p.ctl._logoutPrincipal(), null);
  assert.ok(!JSON.stringify([p.view(), p.storage.data, p.ctl._state()]).includes("пока выхожу"));
};

journeys.a_sign_out_whose_request_was_lost_is_repeated_and_ends_the_session = async ({ base, cookie }) => {
  const jar = { cookie };
  const p = page(base, jar);
  await p.boot();
  await (await p.ctl.send("пока не вышла")).done;

  p.net.dropRequest(isLogout);
  assert.equal(await p.ctl.logout(), false);
  assert.equal((await p.net.raw("GET", "/auth/me")).status, 200, "it never reached the server: the session is open");

  assert.equal(await p.ctl.recover(), true);
  assert.deepEqual(logouts(p).map((r) => r.status), [null, 204]);
  assert.equal((await p.net.raw("GET", "/auth/me")).status, 401);
  assert.deepEqual([p.view().screen, p.view().notice.text], ["login", Logic.TEXT.loggedOut]);
};


// --- history titles and favorites ---------------------------------------------------------------------------

const assistantOf = (p) => p.items().find((item) => item.role === "assistant");

journeys.a_long_first_message_is_named_the_same_by_the_page_and_by_the_server = async ({ base, cookie }) => {
  const question = "  Подскажите,   пожалуйста,\nкак приготовить домашнюю лапшу 🍜 для куриного супа без яиц и без муки высшего сорта  ";
  const first = page(base, cookie);
  await first.boot();
  await (await first.ctl.send(question)).done;
  const named = first.view().list.items[0].label;

  const reloaded = page(base, cookie); // the name now comes from the server's own rule
  const view = await reloaded.boot();

  assert.equal(view.list.items.length, 1);
  assert.equal(view.list.items[0].label, named);
  assert.equal(view.chatTitle, named);
  assert.equal(named, Logic.deriveTitle(question));
  assert.ok(named.endsWith("…") && !/[\n\t]/.test(named) && Array.from(named).length === Logic.TITLE_LENGTH + 1, named);
};

journeys.favorites_are_kept_reopened_after_a_reload_and_removed = async ({ base, cookie }) => {
  const first = page(base, cookie);
  await first.boot();
  await (await first.ctl.send("Как варить борщ?")).done;
  const conversation = first.view().currentId;
  const answer = assistantOf(first);
  assert.equal(answer.favorite.state, "none");

  assert.deepEqual(await first.ctl.keepFavorite(conversation, answer.id), { ok: true, reason: "created" });
  assert.equal(assistantOf(first).favorite.state, "saved");

  const second = page(base, cookie); // a new tab: nothing in memory
  let view = await second.boot();
  assert.deepEqual([view.favorites.status, view.favorites.items.length], ["ready", 1]);
  assert.equal(view.favorites.items[0].title, "Ответ на: Как варить борщ?");
  assert.equal(assistantOf(second).favorite.state, "saved", "the answer is marked from the first read");
  const id = view.favorites.items[0].id;
  assert.equal(assistantOf(second).favorite.id, id);

  await second.ctl.openFavorite(id);
  await second.ctl.idle();
  view = second.view();
  assert.deepEqual([view.section, view.favorites.current.status, view.favorites.current.text], ["favorites", "ready", "Ответ на: Как варить борщ?"]);

  // The server keeps one favorite per answer: asking again, outside the page's own check, is the same one.
  const again = await second.net.raw("POST", "/api/favorites", { conversation_id: conversation, message_id: answer.id });
  assert.deepEqual([again.status, again.json.created, again.json.id], [200, false, id]);

  second.ctl.askRemoveFavorite(id);
  assert.deepEqual(await second.ctl.removeFavorite(), { ok: true });
  assert.deepEqual(second.view().favorites.items, []);

  const third = page(base, cookie);
  view = await third.boot();
  assert.deepEqual([view.favorites.status, view.favorites.items.length], ["ready", 0]);
  assert.equal(assistantOf(third).favorite.state, "none");
  assert.deepEqual(third.texts(), ["Как варить борщ?", "Ответ на: Как варить борщ?"], "the conversation is as it was");
  const gone = await third.net.raw("DELETE", "/api/favorites/" + id);
  assert.equal(gone.status, 404);
  assert.equal(gone.json.detail, "favorite_not_found");
  return { favorite: id };
};

journeys.favorites_belong_to_the_person_who_kept_them = async ({ base, cookie, other }) => {
  const anna = page(base, cookie);
  await anna.boot();
  await (await anna.ctl.send("секрет рецепт Анны")).done;
  const conversation = anna.view().currentId;
  const answer = assistantOf(anna);
  await anna.ctl.keepFavorite(conversation, answer.id);
  const id = anna.view().favorites.items[0].id;

  const boris = page(base, other); // his own browser
  const view = await boris.boot();
  assert.deepEqual([view.favorites.status, view.favorites.items], ["ready", []]);
  assert.equal(view.list.items.length, 0);
  assert.equal((await boris.net.raw("GET", "/api/favorites/" + id)).status, 404);
  assert.equal((await boris.net.raw("DELETE", "/api/favorites/" + id)).status, 404);
  assert.equal((await boris.net.raw("GET", "/api/conversations/" + conversation + "/messages")).status, 404);
  const stolen = await boris.net.raw("POST", "/api/favorites", { conversation_id: conversation, message_id: answer.id });
  assert.deepEqual([stolen.status, stolen.json.detail], [404, "conversation_not_found"]);
  await boris.ctl.openFavorite(id); // the page itself, asked for her recipe by id
  await boris.ctl.idle();
  assert.equal(boris.view().favorites.current.status, "gone");
  assert.ok(!JSON.stringify([boris.view(), boris.ctl._state()]).includes("секрет рецепт"));

  const again = page(base, cookie);
  const back = await again.boot();
  assert.deepEqual(back.favorites.items.map((item) => item.id), [id], "nothing of hers was taken or removed");
};

// --- run -------------------------------------------------------------------------------------------------------

(async () => {
  const [name, base, cookie, other] = process.argv.slice(2);
  if (name === "--list") {
    console.log(JSON.stringify({ names: Object.keys(journeys) }));
    return;
  }
  try {
    const data = await journeys[name]({ base, cookie, other });
    console.log(JSON.stringify({ ok: true, data: data || null }));
  } catch (error) {
    console.log(JSON.stringify({ ok: false, error: String((error && error.stack) || error).split("\n").slice(0, 14).join("\n") }));
  }
})();
