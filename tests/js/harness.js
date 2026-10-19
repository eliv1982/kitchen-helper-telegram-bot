"use strict";
/*
 * Fakes for testing house_helper/web/static/chat-logic.js in Node, with nothing real:
 * a scripted fetch (the server's answers are written by the test, in the shapes the real
 * API uses), a clock and timers the test moves, a storage that can be broken, and a
 * "cryptographic" generator that counts. Run by tests/test_web_ui_logic.py.
 */

const path = require("node:path");

const Logic = require(path.join(__dirname, "..", "..", "house_helper", "web", "static", "chat-logic.js"));

const CONV_A = "11111111-1111-4111-8111-111111111111";
const CONV_B = "22222222-2222-4222-8222-222222222222";
const CONV_C = "33333333-3333-4333-8333-333333333333";
const START = Date.UTC(2026, 9, 18, 12, 0, 0);

async function flush(rounds = 25) {
  for (let i = 0; i < rounds; i++) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

// --- time -----------------------------------------------------------------------------

class Timers {
  constructor() {
    this.now = START;
    this.queue = [];
    this.seq = 0;
    this.setTimeout = (fn, ms) => {
      const id = ++this.seq;
      this.queue.push({ id, at: this.now + ms, fn, ms });
      return id;
    };
    this.clearTimeout = (id) => {
      this.queue = this.queue.filter((timer) => timer.id !== id);
    };
    // (only the page's one-second ticker uses these; the logic never does)
    this.setInterval = (fn, ms) => {
      const id = ++this.seq;
      this.queue.push({ id, at: this.now + ms, fn, ms, repeat: ms });
      return id;
    };
    this.clearInterval = this.clearTimeout;
  }

  get pending() {
    return this.queue.length;
  }

  // The delays of the timers that are waiting (to see what the page asked to wait for).
  get delays() {
    return this.queue.map((timer) => timer.ms).sort((a, b) => a - b);
  }

  async advance(ms) {
    const target = this.now + ms;
    for (;;) {
      this.queue.sort((a, b) => a.at - b.at || a.id - b.id);
      const next = this.queue[0];
      if (!next || next.at > target) {
        break;
      }
      this.queue.shift();
      this.now = Math.max(this.now, next.at);
      if (next.repeat) this.queue.push({ ...next, at: next.at + next.repeat });
      next.fn();
      await flush();
    }
    this.now = target;
    await flush();
  }
}

// --- storage and randomness ----------------------------------------------------------------

class Storage {
  constructor(initial = {}) {
    this.data = { ...initial };
    this.broken = false;
  }

  getItem(key) {
    if (this.broken) throw new Error("SecurityError");
    return Object.prototype.hasOwnProperty.call(this.data, key) ? this.data[key] : null;
  }

  setItem(key, value) {
    if (this.broken) throw new Error("QuotaExceededError");
    this.data[key] = String(value);
  }

  removeItem(key) {
    if (this.broken) throw new Error("SecurityError");
    delete this.data[key];
  }

  get record() {
    const raw = this.data[Logic.STORAGE_KEY];
    return raw === undefined ? null : JSON.parse(raw);
  }
}

function fakeCrypto() {
  let counter = 0;
  return {
    calls: 0,
    getRandomValues(array) {
      this.calls++;
      counter++;
      for (let i = 0; i < array.length; i++) {
        array[i] = (counter * 31 + i * 7 + 1) & 0xff;
      }
      array[0] = counter & 0xff;
      array[1] = (counter >> 8) & 0xff;
      return array;
    },
  };
}

// --- the network -----------------------------------------------------------------------------

function json(status, body, headers = {}) {
  return { kind: "json", status, body, headers };
}

function text(status, body, headers = {}) {
  return { kind: "text", status, body, headers };
}

const fail = () => ({ kind: "fail" });
const hang = () => ({ kind: "hang" });
const dropBody = (status = 200, headers = {}) => ({ kind: "dropBody", status, headers });

let correlation = 0;
function nextCorrelation() {
  correlation++;
  return "hh-" + correlation.toString(16).padStart(16, "0");
}

class Net {
  constructor() {
    this.routes = [];
    this.log = [];
    this.unscripted = [];
    this.fetch = this.fetch.bind(this);
  }

  // route("GET", "/api/conversations", responder, responder, ...): the responders answer in
  // turn, and the last one goes on answering. A responder may be a function of the request.
  route(method, pattern, ...responders) {
    this.routes.unshift({ method, pattern, responders, used: 0 });
    return this;
  }

  requests(method, pattern) {
    return this.log.filter((r) => r.method === method && matches(pattern, r.url));
  }

  async fetch(url, init = {}) {
    const method = (init.method || "GET").toUpperCase();
    const headers = {};
    for (const [name, value] of Object.entries(init.headers || {})) {
      headers[name.toLowerCase()] = value;
    }
    let body = init.body;
    if (typeof body === "string") {
      try {
        body = JSON.parse(body);
      } catch (ignored) {
        // left as text
      }
    }
    const request = { method, url, headers, body, init, signal: init.signal };
    this.log.push(request);
    const route = this.routes.find((r) => r.method === method && matches(r.pattern, url));
    if (!route) {
      this.unscripted.push(`${method} ${url}`);
      throw new TypeError("unscripted request: " + method + " " + url);
    }
    let responder = route.responders[Math.min(route.used, route.responders.length - 1)];
    route.used++;
    if (typeof responder === "function") {
      responder = await responder(request);
    }
    return respond(responder, init.signal);
  }
}

function matches(pattern, url) {
  return pattern instanceof RegExp ? pattern.test(url) : url === pattern || url.startsWith(pattern + "?");
}

function respond(responder, signal) {
  switch (responder.kind) {
    case "json":
      return new Response(JSON.stringify(responder.body), {
        status: responder.status,
        headers: { "Content-Type": "application/json", ...responder.headers },
      });
    case "text":
      return new Response(responder.body, { status: responder.status, headers: responder.headers });
    case "fail":
      throw new TypeError("Failed to fetch");
    case "dropBody":
      return {
        status: responder.status,
        headers: { get: () => null },
        text: () => Promise.reject(new TypeError("terminated")),
      };
    case "hang":
      return new Promise((resolve, reject) => {
        const abort = () => {
          const error = new Error("The operation was aborted.");
          error.name = "AbortError";
          reject(error);
        };
        if (signal && signal.aborted) abort();
        else if (signal) signal.addEventListener("abort", abort);
      });
    default:
      throw new Error("unknown responder " + responder.kind);
  }
}

// --- what the server says, in the shapes of the real API ---------------------------------------------

function iso(seconds) {
  return new Date(START - 3600_000 + seconds * 1000).toISOString().replace("Z", "+00:00");
}

function conversation(id, minutes = 0, title = null) {
  const at = new Date(START - 7200_000 + minutes * 60_000).toISOString().replace("Z", "+00:00");
  return { id, title, created_at: at, updated_at: at };
}

const listBody = (conversations, hasMore = false) => json(200, { conversations, has_more: hasMore });

function message(id, role, textValue, extra = {}) {
  return { id, role, text: textValue, created_at: iso(id), status: "completed", ...extra };
}

function historyBody(id, messages, { hasMore = false, nextBefore = null } = {}) {
  return json(200, {
    conversation_id: id,
    messages,
    has_more: hasMore,
    next_before: hasMore ? nextBefore ?? messages[0].id : null,
  });
}

function turnBody(id, userId, userText, assistantId, assistantText, replayed = false) {
  const body = {
    conversation_id: id,
    user_message: { id: userId, role: "user", text: userText, created_at: iso(userId), status: "completed" },
    assistant_message: { id: assistantId, role: "assistant", text: assistantText, created_at: iso(assistantId), status: "completed" },
    truncated: false,
    replayed,
  };
  return json(replayed ? 200 : 201, body, replayed ? { "Idempotent-Replayed": "true" } : {});
}

const CATALOGUE_MESSAGES = Logic.CODE_MESSAGES;

function apiError(status, code, extra = {}, headers = {}) {
  const retryable = extra.retryable ?? false;
  const body = {
    detail: code,
    message: extra.message ?? CATALOGUE_MESSAGES[code] ?? "Ошибка.",
    retryable,
    correlation_id: extra.correlation_id ?? nextCorrelation(),
  };
  if (extra.retry_after_seconds !== undefined) body.retry_after_seconds = extra.retry_after_seconds;
  if (extra.user_message_id !== undefined) body.user_message_id = extra.user_message_id;
  const answerHeaders = { "X-Correlation-ID": body.correlation_id, ...headers };
  if (extra.retry_after_seconds !== undefined) answerHeaders["Retry-After"] = String(extra.retry_after_seconds);
  return json(status, body, answerHeaders);
}

const busy = (code = "conversation_busy") =>
  apiError(409, code, { retryable: true, retry_after_seconds: 5 });

// Two people who can be signed in to the same browser, one after the other.
const USER_A = "99999999-9999-4999-8999-999999999999";
const USER_B = "88888888-8888-4888-8888-888888888888";

// The session ends `seconds` after the clock's start (a day by default; the real one lasts 14 days).
const me = (seconds = 86400, userId = USER_A) =>
  json(200, {
    user_id: userId,
    session_expires_at: new Date(START + seconds * 1000).toISOString().replace("Z", "+00:00"),
  });
const notAuthenticated = () => json(401, { detail: "not_authenticated" });

// --- the environment of one scenario ----------------------------------------------------------------

const environments = [];

function makeEnv(options = {}) {
  const timers = new Timers();
  const net = new Net();
  const storage = options.storage || new Storage();
  const crypto = options.crypto === undefined ? fakeCrypto() : options.crypto;
  const changes = { count: 0 };
  const announced = [];
  const controller = Logic.createController({
    fetch: net.fetch,
    storage: options.noStorage ? null : storage,
    crypto,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    now: () => timers.now,
    AbortController,
    maxLength: options.maxLength,
    onChange: () => {
      changes.count++;
    },
    announce: (line) => announced.push(line),
  });
  const env = {
    ctl: controller,
    net,
    timers,
    storage,
    crypto,
    changes,
    announced,
    view: () => controller.getView(),
    async settle() {
      await controller.idle();
      await flush();
      await controller.idle();
    },
    // The server of a signed-in person: who they are, their conversations, each one's history.
    signedIn(conversations = [], histories = {}) {
      net.route("GET", "/auth/me", me());
      net.route("GET", "/api/conversations", listBody(conversations));
      for (const c of conversations) {
        net.route(
          "GET",
          new RegExp("/api/conversations/" + c.id + "/messages(\\?|$)"),
          histories[c.id] || historyBody(c.id, [])
        );
      }
      return env;
    },
    async boot() {
      await controller.start();
      await env.settle();
      return env.view();
    },
    posts(conversationId) {
      return net.log.filter((r) => r.method === "POST" && r.url === `/api/conversations/${conversationId}/messages`);
    },
    keys(conversationId) {
      return env.posts(conversationId).map((r) => r.headers["idempotency-key"]);
    },
  };
  environments.push(env);
  return env;
}

module.exports = {
  Logic,
  CONV_A,
  CONV_B,
  CONV_C,
  START,
  flush,
  Timers,
  Storage,
  fakeCrypto,
  Net,
  json,
  text,
  fail,
  hang,
  dropBody,
  iso,
  conversation,
  listBody,
  message,
  historyBody,
  turnBody,
  apiError,
  busy,
  me,
  USER_A,
  USER_B,
  notAuthenticated,
  makeEnv,
  environments,
};
