"use strict";
/*
 * A minimal DOM, just enough to run house_helper/web/static/app.js in Node: elements that
 * keep attributes, children and listeners, text nodes, getElementById, and a tiny HTML parser
 * for the shell the server sends (house_helper/web/ui.py renders it; tests/test_web_ui_dom.py
 * hands it over). It is NOT a browser: nothing is laid out, nothing is styled, no event
 * bubbles. It records every attribute app.js sets, so a test can check that none is an
 * event handler or a style.
 */

const VOID = new Set(["meta", "link", "input", "br", "img", "hr", "base"]);

class FakeNode {
  constructor() {
    this.parentNode = null;
    this.childNodes = [];
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index < 0) throw new Error("not a child");
    this.childNodes.splice(index, 1);
    child.parentNode = null;
    return child;
  }
}

class FakeText extends FakeNode {
  constructor(data) {
    super();
    this.nodeType = 3;
    this.data = String(data);
  }

  get textContent() {
    return this.data;
  }
}

class FakeElement extends FakeNode {
  constructor(tag, document) {
    super();
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.localName = tag.toLowerCase();
    this.ownerDocument = document;
    this.attrs = new Map();
    this.listeners = {};
    this.style = {};
    this.value = "";
    this.readOnly = false;
    this.inert = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
  }

  setAttribute(name, value) {
    this.ownerDocument.attributeLog.push([this.localName, name, String(value)]);
    this.attrs.set(name, String(value));
    if (name === "id") this.ownerDocument.registry.set(String(value), this);
  }

  getAttribute(name) {
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }

  hasAttribute(name) {
    return this.attrs.has(name);
  }

  removeAttribute(name) {
    this.attrs.delete(name);
  }

  get id() {
    return this.getAttribute("id") || "";
  }

  get className() {
    return this.getAttribute("class") || "";
  }

  set className(value) {
    this.attrs.set("class", String(value)); // (the property, not setAttribute: it is not an attribute the script chose by name)
  }

  get hidden() {
    return this.attrs.has("hidden");
  }

  set hidden(value) {
    if (value) this.attrs.set("hidden", "");
    else this.attrs.delete("hidden");
  }

  get textContent() {
    return this.childNodes.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.childNodes.forEach((child) => (child.parentNode = null));
    this.childNodes = [];
    if (value !== "" && value !== null && value !== undefined) this.appendChild(new FakeText(value));
  }

  addEventListener(type, listener) {
    (this.listeners[type] = this.listeners[type] || []).push(listener);
  }

  // The event an element hands to its listeners (no bubbling here).
  fire(type, init = {}) {
    const event = {
      type,
      target: this,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
      ...init,
    };
    for (const listener of this.listeners[type] || []) listener.call(this, event);
    return event;
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

  closest(tag) {
    for (let node = this; node; node = node.parentNode) {
      if (node.localName === tag) return node;
    }
    return null;
  }

  // A click: the listeners, then what a browser does for a submit button in a form.
  click() {
    const event = this.fire("click");
    if (event.defaultPrevented) return;
    if (this.localName === "button" && (this.getAttribute("type") || "submit") === "submit") {
      const form = this.closest("form");
      if (form) form.fire("submit");
    }
  }

  *descendants() {
    for (const child of this.childNodes) {
      yield child;
      if (child.nodeType === 1) yield* child.descendants();
    }
  }

  elements() {
    return [...this.descendants()].filter((node) => node.nodeType === 1);
  }

  find(predicate) {
    return this.elements().filter(predicate);
  }

  byClass(name) {
    return this.find((element) => element.className.split(/\s+/).includes(name));
  }

  byTag(tag) {
    return this.find((element) => element.localName === tag);
  }

  buttons(label) {
    return this.byTag("button").filter((b) => b.textContent === label);
  }
}

class FakeDocument {
  constructor() {
    this.registry = new Map();
    this.attributeLog = [];
    this.listeners = {};
    this.activeElement = null;
    this.visibilityState = "visible";
    this.body = null;
  }

  createElement(tag) {
    return new FakeElement(tag, this);
  }

  createTextNode(data) {
    return new FakeText(data);
  }

  getElementById(id) {
    return this.registry.get(id) || null;
  }

  addEventListener(type, listener) {
    (this.listeners[type] = this.listeners[type] || []).push(listener);
  }

  fire(type, init = {}) {
    for (const listener of this.listeners[type] || []) listener({ type, preventDefault() {}, ...init });
  }
}

const ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " " };
const decode = (text) => text.replace(/&(?:amp|lt|gt|quot|#39|nbsp);/g, (entity) => ENTITIES[entity]);

// Parse the server's shell into a FakeDocument (what the script would find in the page).
function parseShell(html) {
  const document = new FakeDocument();
  const root = document.createElement("#root");
  const stack = [root];
  const token = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>|([^<]+)/gi;
  let match;
  while ((match = token.exec(html)) !== null) {
    const [whole, closing, opening, attributes, text] = match;
    if (closing) {
      const index = stack.map((e) => e.localName).lastIndexOf(closing.toLowerCase());
      if (index > 0) stack.length = index;
    } else if (opening) {
      const element = document.createElement(opening.toLowerCase());
      const attributePattern = /([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let attribute;
      while ((attribute = attributePattern.exec(attributes || "")) !== null) {
        const value = attribute[2] ?? attribute[3] ?? attribute[4] ?? "";
        element.attrs.set(attribute[1], decode(value));
        if (attribute[1] === "id") document.registry.set(decode(value), element);
      }
      stack[stack.length - 1].appendChild(element);
      if (!VOID.has(opening.toLowerCase()) && !/\/>$/.test(whole)) stack.push(element);
      if (opening.toLowerCase() === "body") document.body = element;
    } else if (text !== undefined) {
      stack[stack.length - 1].appendChild(new FakeText(decode(text)));
    }
  }
  document.attributeLog.length = 0; // what the parser set is the server's, not the script's
  document.root = root;
  return document;
}

module.exports = { FakeDocument, FakeElement, FakeText, parseShell };
