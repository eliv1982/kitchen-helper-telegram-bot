/*
 * Kitchen Helper web chat: the page.
 *
 * The logic (what to send, what a reply means, how to recover from an unclear one) is in
 * chat-logic.js; this file draws what controller.getView() describes and forwards clicks
 * and keys to it. All text that came from anywhere -- the person, the assistant, the
 * server -- goes into the page as a text node (textContent), never as markup: there is no
 * innerHTML, no insertAdjacentHTML, no eval and no inline handler anywhere. The facts the
 * server knows before anyone has signed in (which ways of signing in exist, the length
 * limit) are the data attributes of <body>, written by the server (ui.py).
 */
(function () {
  "use strict";

  var Logic = window.HouseHelperLogic;
  var body = document.body;

  function byId(id) {
    return document.getElementById(id);
  }

  var els = {
    gate: byId("gate"),
    loading: byId("screen-loading"),
    loadingText: byId("loading-text"),
    unavailable: byId("screen-unavailable"),
    unavailableTitle: byId("unavailable-title"),
    unavailableText: byId("unavailable-text"),
    unavailableId: byId("unavailable-id"),
    unavailableRetry: byId("unavailable-retry"),
    setup: byId("screen-setup"),
    setupTitle: byId("setup-title"),
    login: byId("screen-login"),
    loginTitle: byId("login-title"),
    loginNotice: byId("login-notice"),
    emailForm: byId("email-form"),
    emailInput: byId("email-input"),
    emailSubmit: byId("email-submit"),
    emailResult: byId("email-result"),
    app: byId("screen-app"),
    chat: byId("chat"),
    chatTitle: byId("chat-title"),
    drawerToggle: byId("drawer-toggle"),
    sidebar: byId("sidebar"),
    backdrop: byId("backdrop"),
    logout: byId("logout"),
    newConversation: byId("new-conversation"),
    listStatus: byId("list-status"),
    listRetry: byId("list-retry"),
    list: byId("conversation-list"),
    banner: byId("banner"),
    bannerText: byId("banner-text"),
    bannerClose: byId("banner-close"),
    messages: byId("messages"),
    composer: byId("composer"),
    input: byId("composer-input"),
    send: byId("send"),
    composerError: byId("composer-error"),
    composerHint: byId("composer-hint"),
    counter: byId("counter"),
    announcer: byId("announcer"),
    alerter: byId("alerter"),
  };

  function parseLimit(value) {
    var number = parseInt(value, 10);
    return number >= 1 && number <= 20000 ? number : Logic.MAX_MESSAGE_LENGTH;
  }

  var maxLength = parseLimit(body.getAttribute("data-max-length"));

  // --- small helpers ---------------------------------------------------------------------

  // An element with attributes and text. Text only through textContent; no attribute named
  // on* or style (nothing here needs one, and the page's policy would refuse them).
  function h(tag, props, children) {
    var node = document.createElement(tag);
    var names = props ? Object.keys(props) : [];
    for (var i = 0; i < names.length; i++) {
      var name = names[i];
      var value = props[name];
      if (value === false || value === null || value === undefined) {
        continue;
      }
      if (/^(on|style$)/i.test(name)) {
        throw new Error("not allowed: " + name);
      }
      if (name === "class") {
        node.className = value;
      } else if (name === "text") {
        node.textContent = value;
      } else {
        node.setAttribute(name, value === true ? "" : String(value));
      }
    }
    if (children) {
      for (var j = 0; j < children.length; j++) {
        if (children[j]) {
          node.appendChild(children[j]);
        }
      }
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function openSessionStorage() {
    try {
      var storage = window.sessionStorage;
      storage.setItem("hh.probe", "1");
      storage.removeItem("hh.probe");
      return storage;
    } catch (ignored) {
      return null; // private mode, blocked storage: the page works without recovery after a reload
    }
  }

  // (A spoken line is set a moment after it is asked for, so that a screen reader notices the change;
  // if the signed-in page has gone by then, it is not set: nothing of it is put back.)
  var announceTimer = null;
  function announce(text) {
    if (!text) {
      return;
    }
    els.announcer.textContent = "";
    if (announceTimer !== null) {
      window.clearTimeout(announceTimer);
    }
    announceTimer = window.setTimeout(function () {
      if (lastScreen === "app") {
        els.announcer.textContent = text;
      }
    }, 60);
  }

  function alertText(text) {
    els.alerter.textContent = "";
    if (text) {
      window.setTimeout(function () {
        if (lastScreen === "app") {
          els.alerter.textContent = text;
        }
      }, 60);
    }
  }

  // --- the controller --------------------------------------------------------------------------

  var controller = Logic.createController({
    fetch: function (input, init) {
      return window.fetch(input, init);
    },
    storage: openSessionStorage(),
    crypto: window.crypto,
    setTimeout: function (fn, ms) {
      return window.setTimeout(fn, ms);
    },
    clearTimeout: function (id) {
      window.clearTimeout(id);
    },
    now: function () {
      return Date.now();
    },
    maxLength: maxLength,
    onChange: scheduleRender,
    announce: announce,
  });

  // --- drawing ---------------------------------------------------------------------------------------

  var queued = false;
  function scheduleRender() {
    if (!queued) {
      queued = true;
      Promise.resolve().then(function () {
        queued = false;
        render();
      });
    }
  }

  // Each region is redrawn only when what describes it changed.
  var drawn = {};
  function region(name, data, draw) {
    var signature = JSON.stringify(data);
    if (drawn[name] === signature) {
      return false;
    }
    drawn[name] = signature;
    draw(data);
    return true;
  }

  var lastScreen = null;
  var lastView = null;
  // What the person had typed, kept here (not in the page) while the signed-in page is held back to ask
  // who is signed in: given back only if it is the same page (generation) of the same person when it
  // comes back, dropped by any other way out of the held-back screens.
  var heldDraft = null;

  function render() {
    var view = controller.getView();
    lastView = view;
    region("screen", { screen: view.screen, notice: view.notice, failure: view.failure, generation: view.generation }, function (data) {
      drawScreens(data);
    });
    if (view.screen === "app") {
      region("list", view.list, drawList);
      region("title", view.chatTitle, function (title) {
        els.chatTitle.textContent = title;
      });
      region("banner", view.banner, drawBanner);
      region("thread", view.thread, function (thread) {
        drawThread(thread, view.currentId);
      });
      region("composer", view.composer, drawComposer);
    }
  }

  // The screens of the gate: waiting for the server (the words differ), and saying something could not be done.
  var WAITING = { loading: Logic.TEXT.loading, verifying: Logic.TEXT.verifying, signing_out: Logic.TEXT.signingOut };

  function drawScreens(data) {
    var screen = data.screen;
    var waiting = Object.prototype.hasOwnProperty.call(WAITING, screen);
    var apart = screen === "unavailable" || screen === "unverified";
    els.gate.hidden = screen === "app";
    els.loading.hidden = !waiting;
    els.unavailable.hidden = !apart;
    els.setup.hidden = screen !== "setup";
    els.login.hidden = screen !== "login";
    els.app.hidden = screen !== "app";
    els.chat.hidden = screen !== "app";
    els.gate.setAttribute("aria-busy", waiting ? "true" : "false");

    if (waiting) {
      els.loadingText.textContent = WAITING[screen];
    }
    if (apart) {
      els.unavailableText.textContent = data.failure ? data.failure.text : Logic.TEXT.unavailable;
      var id = data.failure ? data.failure.correlationId : null;
      els.unavailableId.hidden = !id;
      els.unavailableId.textContent = id ? Logic.TEXT.correlation.replace("{id}", id) : "";
      els.unavailableRetry.textContent = data.failure && data.failure.retry === "logout" ? Logic.TEXT.logoutAgain : Logic.TEXT.retry;
    }
    if (screen === "login") {
      drawNotice(els.loginNotice, data.notice);
    }
    if (screen !== lastScreen) {
      var held = screen === "verifying" || screen === "unverified";
      if (held && lastScreen === "app") {
        heldDraft = { generation: data.generation, text: els.input.value };
      }
      if (screen === "login") {
        els.loginTitle.focus();
      } else if (screen === "setup") {
        els.setupTitle.focus();
      } else if (apart) {
        els.unavailableTitle.focus();
      } else if (screen === "app") {
        setDrawer(false, false);
      }
      if (screen === "app") {
        // Back from being held back: the same page of the same person gets its draft back; anything else does not.
        if (heldDraft !== null && heldDraft.generation === data.generation && heldDraft.text) {
          els.input.value = heldDraft.text;
          autosize();
          updateCounter();
        }
        heldDraft = null;
      } else if (!held) {
        heldDraft = null;
      }
      if (screen !== "app") {
        // Nothing of the last person stays on screen or in the composer, hidden or not: the
        // conversation, the list, the title, every line the signed-in page wrote about them.
        drawn.thread = drawn.list = drawn.banner = drawn.composer = drawn.title = null;
        clear(els.messages);
        clear(els.list);
        els.input.value = "";
        autosize();
        els.chatTitle.textContent = "";
        els.listStatus.textContent = "";
        els.bannerText.textContent = "";
        els.banner.hidden = true;
        els.composerHint.textContent = "";
        els.composerError.textContent = "";
        els.composerError.hidden = true;
        els.counter.textContent = "";
        els.announcer.textContent = "";
        els.alerter.textContent = "";
        lastThread = { id: null, first: null, last: null };
        lastPendingNote = "";
      }
      lastScreen = screen;
    }
  }

  function drawNotice(node, notice) {
    if (!notice) {
      node.hidden = true;
      node.textContent = "";
      node.className = "notice";
      return;
    }
    node.hidden = false;
    node.textContent = notice.text;
    node.className = "notice" + (notice.kind === "error" ? " notice-error" : notice.kind === "ok" ? " notice-ok" : "");
  }

  function drawBanner(banner) {
    if (!banner) {
      els.banner.hidden = true;
      els.bannerText.textContent = "";
      return;
    }
    els.banner.hidden = false;
    els.banner.className = "banner" + (banner.kind === "error" ? " banner-error" : "");
    els.bannerText.textContent = banner.text;
    if (banner.correlationId) {
      els.bannerText.appendChild(document.createElement("br"));
      els.bannerText.appendChild(
        h("span", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", banner.correlationId) })
      );
    }
    if (banner.kind === "error") {
      alertText(banner.text);
    } else {
      announce(banner.text);
    }
  }

  // The list of conversations ---------------------------------------------------------------------

  function drawList(list) {
    var status = "";
    els.listRetry.hidden = true;
    if (list.status === "loading" && list.items.length === 0) {
      status = "Загружаем разговоры…";
    } else if (list.status === "error") {
      status = list.error ? list.error.text : Logic.TEXT.listFailed;
      els.listRetry.hidden = false;
    } else if (list.status === "ready" && list.items.length === 0) {
      status = "Разговоров пока нет. Начните новый.";
    } else if (list.hasMore) {
      status = "Показаны последние " + Logic.PAGE_SIZE + " разговоров.";
    }
    els.listStatus.textContent = status;
    els.newConversation.setAttribute("aria-current", list.draftCurrent ? "true" : "false");
    clear(els.list);
    list.items.forEach(function (item) {
      var button = h("button", { class: "conv-item", type: "button", "aria-current": item.current ? "true" : false }, [
        h("span", { class: "conv-label", text: item.label }),
        h("span", { class: "conv-meta", text: item.meta }),
      ]);
      button.addEventListener("click", function () {
        selectConversation(item.id);
      });
      els.list.appendChild(h("li", null, [button]));
    });
  }

  // The messages ------------------------------------------------------------------------------------------

  // Paragraphs by blank lines; single line breaks stay (white-space: pre-wrap in the style). The
  // text is a text node: whatever it contains is shown, not interpreted.
  function textBlocks(text) {
    var wrapper = h("div", { class: "msg-text" });
    var parts = String(text).replace(/\r\n?/g, "\n").split(/\n{2,}/);
    parts.forEach(function (part) {
      var trimmed = part.replace(/^\n+|\n+$/g, "");
      if (trimmed) {
        wrapper.appendChild(h("p", { text: trimmed }));
      }
    });
    return wrapper;
  }

  function action(label, className, handler) {
    var button = h("button", { class: "btn " + className, type: "button", text: label });
    button.addEventListener("click", handler);
    return button;
  }

  function messageNode(item, conversationId) {
    var mine = item.role === "user";
    var meta = h("div", { class: "msg-meta" }, [
      item.time ? h("time", { datetime: item.iso || false, text: item.time }) : null,
    ]);
    var node = h("article", {
      class: "msg " + (mine ? "msg-user" : "msg-assistant") + (item.status === "failed" ? " msg-failed" : ""),
      "data-id": item.type === "message" ? String(item.id) : false,
    });
    node.appendChild(h("p", { class: "visually-hidden", text: mine ? "Вы:" : "Помощник:" }));
    node.appendChild(textBlocks(item.text));
    node.appendChild(meta);

    if (item.type === "message") {
      if (item.note) {
        node.appendChild(h("p", { class: "msg-note" + (item.status === "failed" ? " msg-note-error" : ""), text: item.note }));
      }
      if (item.correlationId) {
        node.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", item.correlationId) }));
      }
      if (item.canRetry) {
        var row = h("div", { class: "msg-actions" });
        row.appendChild(
          action("Повторить", "btn-secondary", function () {
            controller.retryMessage(conversationId);
          })
        );
        node.appendChild(row);
      }
      return node;
    }

    // The message that is being sent, or whose fate is not settled.
    node.className += " msg-pending msg-phase-" + item.phase;
    var note = h("p", { class: "msg-note" + (item.phase === "sending" || item.phase === "waiting" ? "" : " msg-note-error") }, [
      item.busy ? h("span", { class: "spinner", "aria-hidden": "true" }) : null,
      h("span", { text: item.note }),
    ]);
    node.appendChild(note);
    if (item.warning) {
      node.appendChild(h("p", { class: "hint", text: item.warning }));
    }
    if (item.correlationId) {
      node.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", item.correlationId) }));
    }
    if (item.actions.length) {
      var actions = h("div", { class: "msg-actions" });
      item.actions.forEach(function (name) {
        if (name === "retry") {
          actions.appendChild(
            action("Повторить", "btn-primary", function () {
              controller.retry(conversationId);
            })
          );
        } else if (name === "asnew") {
          actions.appendChild(
            action("Отправить как новое сообщение", "btn-primary", function () {
              controller.sendAsNew(conversationId);
            })
          );
        } else if (name === "edit") {
          actions.appendChild(
            action("Вернуть в поле ввода", "btn-quiet", function () {
              takeBack(conversationId);
            })
          );
        }
      });
      node.appendChild(actions);
    }
    return node;
  }

  var lastThread = { id: null, first: null, last: null };
  var lastPendingNote = "";

  function nearBottom() {
    var box = els.messages;
    return box.scrollHeight - box.scrollTop - box.clientHeight < 96;
  }

  var SUGGESTIONS = [
    "Что приготовить из курицы и риса?",
    "Как приготовить борщ?",
    "Чем заменить сметану в рецепте?",
  ];

  function drawThread(thread, conversationId) {
    var box = els.messages;
    var changedConversation = lastThread.id !== conversationId;
    var wasNearBottom = nearBottom();
    var previousHeight = box.scrollHeight;
    var previousTop = box.scrollTop;
    clear(box);

    if (thread.status === "loading") {
      box.appendChild(
        h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode("Загружаем разговор…")])
      );
    } else if (thread.status === "error") {
      box.appendChild(
        h("div", { class: "empty" }, [
          h("p", { class: "notice notice-error", text: thread.error ? thread.error.text : Logic.TEXT.threadFailed }),
          thread.error && thread.error.correlationId
            ? h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", thread.error.correlationId) })
            : null,
          action("Повторить", "btn-primary", function () {
            controller.reloadThread();
          }),
        ])
      );
      alertText(thread.error ? thread.error.text : "");
    } else {
      if (thread.hasMore) {
        var older = h("div", { class: "older" });
        if (thread.loadingOlder) {
          older.appendChild(
            h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode("Загружаем…")])
          );
        } else {
          older.appendChild(
            action("Показать более ранние сообщения", "btn-quiet", function () {
              controller.loadOlder();
            })
          );
        }
        if (thread.olderError) {
          older.appendChild(h("p", { class: "notice notice-error", text: thread.olderError.text }));
        }
        box.appendChild(older);
      }
      if (thread.empty) {
        var empty = h("div", { class: "empty" }, [
          h("p", { class: "lead", text: Logic.TEXT.emptyThread }),
        ]);
        var chips = h("div", { class: "chips" });
        SUGGESTIONS.forEach(function (suggestion) {
          var chip = h("button", { class: "chip", type: "button", text: suggestion });
          chip.addEventListener("click", function () {
            els.input.value = suggestion;
            autosize();
            updateCounter();
            els.input.focus();
          });
          chips.appendChild(chip);
        });
        empty.appendChild(chips);
        box.appendChild(empty);
      }
      thread.items.forEach(function (item) {
        box.appendChild(messageNode(item, conversationId));
      });
      if (thread.error) {
        box.appendChild(
          h("div", { class: "notice notice-error" }, [
            h("span", { text: thread.error.text + " " }),
            action("Обновить", "btn-quiet", function () {
              controller.reloadThread();
            }),
          ])
        );
      }
      if (thread.stalled) {
        box.appendChild(
          h("div", { class: "notice" }, [
            h("span", { text: "Ответ готовится дольше обычного. " }),
            action("Обновить", "btn-quiet", function () {
              controller.reloadThread();
            }),
          ])
        );
      }
    }

    // What to say aloud about the message in flight.
    var pending = null;
    thread.items.forEach(function (item) {
      if (item.type === "pending") {
        pending = item;
      }
    });
    var note = pending ? pending.note : "";
    if (note && note !== lastPendingNote) {
      if (pending.phase === "uncertain" || pending.phase === "failed" || pending.phase === "rejected") {
        alertText(note);
      } else {
        announce(note);
      }
    }
    lastPendingNote = note;

    // Where to be scrolled to afterwards.
    var firstId = thread.items.length ? String(thread.items[0].id) : null;
    var lastItem = thread.items.length ? thread.items[thread.items.length - 1] : null;
    var lastKey = lastItem ? (lastItem.type === "pending" ? "pending" : String(lastItem.id)) : null;
    if (changedConversation || thread.status !== "ready") {
      box.scrollTop = box.scrollHeight;
    } else if (firstId !== lastThread.first && lastKey === lastThread.last) {
      box.scrollTop = previousTop + (box.scrollHeight - previousHeight); // older messages were added above
    } else if (wasNearBottom || (lastItem && lastItem.type === "pending" && lastKey !== lastThread.last)) {
      box.scrollTop = box.scrollHeight;
    } else {
      box.scrollTop = previousTop;
    }
    lastThread = { id: conversationId, first: firstId, last: lastKey };
  }

  // The composer -----------------------------------------------------------------------------------------

  function drawComposer(composer) {
    els.input.readOnly = composer.locked;
    els.send.setAttribute("aria-disabled", composer.locked ? "true" : "false");
    els.send.className = "btn btn-primary btn-send" + (composer.locked ? " is-disabled" : "");
    els.send.textContent = composer.creating ? "Создаём разговор…" : "Отправить";
    els.composerHint.textContent = composer.hint || "";
    els.composerError.hidden = !composer.error;
    els.composerError.textContent = composer.error || "";
    if (composer.error) {
      els.input.setAttribute("aria-invalid", "true");
    } else {
      els.input.removeAttribute("aria-invalid");
    }
    updateCounter();
  }

  function autosize() {
    var input = els.input;
    input.style.height = "auto";
    var wanted = input.scrollHeight + 2;
    input.style.height = Math.min(wanted, 220) + "px";
    input.style.overflowY = wanted > 220 ? "auto" : "hidden"; // no scrollbar until there is something to scroll
  }

  function updateCounter() {
    var count = Logic.countCodePoints(Logic.trimLikeServer(els.input.value));
    if (count === 0) {
      els.counter.textContent = "";
      els.counter.className = "hint counter";
      return;
    }
    els.counter.textContent = count + " / " + maxLength;
    els.counter.className = "hint counter" + (count > maxLength ? " counter-over" : "");
  }

  function takeBack(conversationId) {
    var text = controller.discard(conversationId);
    if (text === null) {
      return;
    }
    var existing = els.input.value;
    els.input.value = existing.trim() ? text + "\n\n" + existing : text;
    autosize();
    updateCounter();
    els.input.focus();
  }

  var sending = false;
  function submit() {
    var view = lastView || controller.getView();
    if (sending || view.composer.locked) {
      return;
    }
    sending = true;
    controller
      .send(els.input.value)
      .then(function (result) {
        sending = false;
        if (result && result.ok) {
          els.input.value = "";
          autosize();
          updateCounter();
        } else if (result && result.reason === "empty") {
          els.input.focus();
        }
      })
      .catch(function () {
        sending = false;
      });
  }

  els.composer.addEventListener("submit", function (event) {
    event.preventDefault();
    submit();
  });

  // Enter makes a new line (on a phone it is the only thing the key can mean); Ctrl+Enter or
  // Cmd+Enter sends.
  els.input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });

  els.input.addEventListener("input", function () {
    autosize();
    updateCounter();
    controller.clearComposerError();
  });

  // --- navigation ----------------------------------------------------------------------------------------------

  var narrow = window.matchMedia ? window.matchMedia("(max-width: 52rem)") : null;

  function setDrawer(open, restoreFocus) {
    var isOpen = Boolean(open) && narrow !== null && narrow.matches;
    els.app.setAttribute("data-drawer", isOpen ? "open" : "closed");
    els.drawerToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    els.backdrop.hidden = !isOpen;
    els.chat.inert = isOpen;
    if (isOpen) {
      els.newConversation.focus();
    } else if (restoreFocus) {
      els.drawerToggle.focus();
    }
  }

  els.drawerToggle.addEventListener("click", function () {
    setDrawer(els.app.getAttribute("data-drawer") !== "open", true);
  });
  els.backdrop.addEventListener("click", function () {
    setDrawer(false, true);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && els.app.getAttribute("data-drawer") === "open") {
      setDrawer(false, true);
    }
  });
  if (narrow && narrow.addEventListener) {
    narrow.addEventListener("change", function () {
      setDrawer(false, false);
    });
  }

  function selectConversation(id) {
    setDrawer(false, false);
    controller.selectConversation(id);
    els.chatTitle.focus();
  }

  els.newConversation.addEventListener("click", function () {
    setDrawer(false, false);
    controller.startDraft();
    els.input.focus();
  });
  els.listRetry.addEventListener("click", function () {
    controller.reloadList();
  });
  els.bannerClose.addEventListener("click", function () {
    controller.dismissBanner();
  });

  els.logout.addEventListener("click", function () {
    if (els.logout.getAttribute("aria-disabled") === "true") {
      return;
    }
    els.logout.setAttribute("aria-disabled", "true");
    controller.logout().then(
      function () {
        els.logout.setAttribute("aria-disabled", "false");
      },
      function () {
        els.logout.setAttribute("aria-disabled", "false");
      }
    );
  });

  // (what it does depends on what the screen says could not be done: see controller.recover)
  els.unavailableRetry.addEventListener("click", function () {
    controller.recover();
  });

  // The send button is not `disabled` while it is locked (a disabled button drops the keyboard
  // focus); the submit handler refuses instead.

  // --- email sign-in --------------------------------------------------------------------------------------------

  if (els.emailForm) {
    var emailBusy = false;
    els.emailForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (emailBusy) {
        return;
      }
      emailBusy = true;
      els.emailSubmit.setAttribute("aria-disabled", "true");
      els.emailResult.hidden = false;
      els.emailResult.className = "notice";
      els.emailResult.textContent = "Отправляем…";
      controller.requestEmailLink(els.emailInput.value).then(
        function (result) {
          emailBusy = false;
          els.emailSubmit.setAttribute("aria-disabled", "false");
          els.emailResult.hidden = false;
          els.emailResult.className = "notice " + (result.ok ? "notice-ok" : "notice-error");
          els.emailResult.textContent = result.text;
          if (result.ok) {
            els.emailSubmit.textContent = "Отправить ссылку ещё раз";
          } else if (result.kind === "invalid") {
            els.emailInput.focus();
          }
        },
        function () {
          emailBusy = false;
          els.emailSubmit.setAttribute("aria-disabled", "false");
          els.emailResult.className = "notice notice-error";
          els.emailResult.textContent = Logic.TEXT.unexpected;
        }
      );
    });
  }

  // --- the clock and the network -----------------------------------------------------------------------------------------

  // Once a second: the session's deadline (a clock comparison, nothing is asked of the server; a
  // machine that slept finds out at its first tick), and, while a message is being sent, the note
  // under it changes with the time (the wait is long).
  window.setInterval(function () {
    controller.checkSession();
    if (lastView && lastView.busy) {
      scheduleRender();
    }
  }, 1000);

  window.addEventListener("online", function () {
    controller.recheck();
  });
  // A page restored from the browser's back/forward cache is as it was when it was left, which may be
  // the page of somebody who has signed out since: ask who is signed in again.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      controller.start();
    }
  });
  // A tab that comes back (or is thawed after being frozen) first checks the deadline, then asks the server.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      controller.recheck();
    }
  });
  document.addEventListener("resume", function () {
    controller.recheck();
  });

  render();
  controller.start();
})();
