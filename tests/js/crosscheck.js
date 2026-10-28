"use strict";
/*
 * Answers questions about chat-logic.js for the Python tests that compare it with the server.
 * Reads one JSON object from stdin ({"task": ..., ...}) and prints one JSON object.
 *
 *   whitespace        every code point that trimLikeServer strips from the ends of a text
 *   count             countCodePoints of each text
 *   prepare           prepareMessage of each text: {ok, reason, text}
 *   title             deriveTitle of each text: the name the page gives a conversation
 *   texts             the page's own words and constants
 */

const path = require("node:path");
const Logic = require(path.join(__dirname, "..", "..", "house_helper", "web", "static", "chat-logic.js"));

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const request = JSON.parse(input);
  let answer;
  switch (request.task) {
    case "whitespace": {
      const stripped = [];
      for (let cp = 0; cp < 0x110000; cp++) {
        const char = String.fromCodePoint(cp);
        if (Logic.trimLikeServer(char + "x" + char) === "x") {
          stripped.push(cp);
        }
      }
      answer = { stripped };
      break;
    }
    case "count":
      answer = { counts: request.texts.map((text) => Logic.countCodePoints(text)) };
      break;
    case "prepare":
      answer = {
        results: request.texts.map((text) => {
          const result = Logic.prepareMessage(text, request.max);
          return { ok: result.ok, reason: result.reason || null, text: result.ok ? result.text : null };
        }),
      };
      break;
    case "title":
      answer = { titles: request.texts.map((text) => Logic.deriveTitle(text)) };
      break;
    case "texts":
      answer = { TEXT: Logic.TEXT, CODE_MESSAGES: Logic.CODE_MESSAGES, MAX: Logic.MAX_MESSAGE_LENGTH, PAGE_SIZE: Logic.PAGE_SIZE };
      break;
    default:
      throw new Error("unknown task " + request.task);
  }
  process.stdout.write(JSON.stringify(answer));
});
