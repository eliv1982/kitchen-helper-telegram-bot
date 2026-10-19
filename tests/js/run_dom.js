"use strict";
/*
 * Runs the scenarios of dom_scenarios.js against the shell in the file given as --shell=PATH
 * (tests/test_web_ui_dom.py writes what house_helper.web.ui.render_shell makes) and prints
 * {"results": {name: {ok, error?}}} as one JSON line. --list prints the names.
 */

process.env.TZ = "UTC";

const fs = require("node:fs");
const assert = require("node:assert/strict");
const H = require("./harness.js");
const { scenarios, useShell } = require("./dom_scenarios.js");

const args = Object.fromEntries(
  process.argv.slice(2).map((arg) => {
    const [name, ...value] = arg.replace(/^--/, "").split("=");
    return [name, value.length ? value.join("=") : true];
  })
);

async function run(name) {
  H.environments.length = 0;
  let handle;
  const timer = new Promise((_, reject) => {
    handle = setTimeout(() => reject(new Error("scenario timed out")), 20000);
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
  useShell(fs.readFileSync(args.shell, "utf8"));
  const results = {};
  for (const name of Object.keys(scenarios)) {
    results[name] = await run(name);
  }
  console.log(JSON.stringify({ results }));
})();
