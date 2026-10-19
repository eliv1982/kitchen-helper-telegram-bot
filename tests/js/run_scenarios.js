"use strict";
/*
 * Runs the scenarios of logic_scenarios.js and prints {"results": {name: {ok, error?}}} as one
 * JSON line. Usage: node run_scenarios.js [--tz=ZONE] [--only=PREFIX] [--list]
 *
 * The time zone is set before anything reads a date (the page shows times in the viewer's
 * zone, and the scenarios that name times say which zone they ran in).
 */

const args = Object.fromEntries(
  process.argv.slice(2).map((arg) => {
    const [name, value] = arg.replace(/^--/, "").split("=");
    return [name, value === undefined ? true : value];
  })
);
process.env.TZ = args.tz || "UTC";

const assert = require("node:assert/strict");
const H = require("./harness.js");
const scenarios = require("./logic_scenarios.js");

const TIMEOUT_MS = 20000;

function selected() {
  const names = Object.keys(scenarios);
  return typeof args.only === "string" ? names.filter((name) => name.startsWith(args.only)) : names;
}

async function run(name) {
  H.environments.length = 0;
  let handle;
  // Not unref()'d: a scenario that waits for something that never comes must be reported, not
  // end the process silently. It is cleared as soon as the scenario is over.
  const timer = new Promise((_, reject) => {
    handle = setTimeout(() => reject(new Error("scenario timed out")), TIMEOUT_MS);
  });
  try {
    await Promise.race([scenarios[name](), timer]);
    for (const env of H.environments) {
      assert.deepEqual(env.net.unscripted, [], "requests nobody scripted");
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String((error && error.stack) || error).split("\n").slice(0, 14).join("\n") };
  } finally {
    clearTimeout(handle);
  }
}

(async () => {
  if (args.list) {
    console.log(JSON.stringify({ names: Object.keys(scenarios) }));
    return;
  }
  const results = {};
  for (const name of selected()) {
    results[name] = await run(name);
  }
  console.log(JSON.stringify({ results, tz: process.env.TZ }));
})();
