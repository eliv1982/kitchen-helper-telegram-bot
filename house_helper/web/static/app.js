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
    helpToggle: byId("help-toggle"),
    helpPanel: byId("help-panel"),
    helpTitle: byId("help-title"),
    helpText: byId("help-text"),
    helpClose: byId("help-close"),
    chatActions: byId("chat-actions"),
    chatActionsToggle: byId("chat-actions-toggle"),
    chatActionsMenu: byId("chat-actions-menu"),
    chatDelete: byId("chat-delete"),
    drawerToggle: byId("drawer-toggle"),
    sidebar: byId("sidebar"),
    backdrop: byId("backdrop"),
    logout: byId("logout"),
    tabChats: byId("tab-chats"),
    tabFavorites: byId("tab-favorites"),
    panelChats: byId("panel-chats"),
    panelFavorites: byId("panel-favorites"),
    newConversation: byId("new-conversation"),
    listStatus: byId("list-status"),
    listRetry: byId("list-retry"),
    list: byId("conversation-list"),
    favoritesStatus: byId("favorites-status"),
    favoritesRetry: byId("favorites-retry"),
    favoriteList: byId("favorite-list"),
    telegram: byId("telegram"),
    telegramText: byId("telegram-text"),
    telegramNotice: byId("telegram-notice"),
    telegramMore: byId("telegram-more"),
    telegramPrivacy: byId("telegram-privacy"),
    telegramOpen: byId("telegram-open"),
    telegramConnect: byId("telegram-connect"),
    telegramDisconnect: byId("telegram-disconnect"),
    telegramConfirm: byId("telegram-confirm"),
    telegramCancel: byId("telegram-cancel"),
    recipe: byId("recipe"),
    recipeTitle: byId("recipe-title"),
    recipeBody: byId("recipe-body"),
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
  // The server's structured-recipe switch (public feature flag): when it is off the page
  // offers no per-recipe Save control, even for an answer that has stored recipes.
  var structuredRecipes = body.getAttribute("data-structured-recipes") === "1";

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
      } else if (name === "value") {
        // A form field's current value, as a property (the attribute alone is only the
        // default value and does not follow what the page means to show).
        node.value = String(value);
        node.setAttribute("value", String(value));
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
    structuredRecipes: structuredRecipes,
    onChange: scheduleRender,
    announce: announce,
  });

  // The panel that connects a Telegram account: its own small controller (chat-logic.js), started when the
  // conversations are shown and stopped when they are not (signed out, another person, held back for a check).
  var telegram = Logic.createTelegramLink({
    fetch: function (input, init) {
      return window.fetch(input, init);
    },
    setTimeout: function (fn, ms) {
      return window.setTimeout(fn, ms);
    },
    clearTimeout: function (id) {
      window.clearTimeout(id);
    },
    now: function () {
      return Date.now();
    },
    onChange: function () {
      scheduleRender();
    },
  });
  var telegramShown = false;

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
    if (view.screen === "app" && !telegramShown) {
      telegramShown = true;
      telegram.start();
    } else if (view.screen !== "app" && telegramShown) {
      telegramShown = false;
      telegram.stop();
    }
    region("telegram", telegramShown ? telegram.getView() : null, drawTelegram);
    if (view.screen === "app") {
      region("section", view.section, drawSection);
      region("list", {
        status: view.list.status,
        error: view.list.error,
        hasMore: view.list.hasMore,
        draftCurrent: view.list.draftCurrent,
        items: view.list.items,
      }, drawList);
      region("favoriteList", { status: view.favorites.status, error: view.favorites.error, items: view.favorites.items }, drawFavoriteList);
      // (The state of the list matters to the pane only while no recipe is open: it then says what to do.)
      var open = view.favorites.current !== null;
      region(
        "recipe",
        { current: view.favorites.current, status: open ? null : view.favorites.status, count: open ? null : view.favorites.items.length },
        drawRecipe
      );
      // The actions belong to an existing conversation that the list still holds (the logic refuses to
      // ask about any other), and only while the conversations are in front. The drawing of the recipe
      // resets focusNodes, so these are drawn after it.
      var canAct = view.currentId !== null && view.list.items.some(function (item) {
        return item.id === view.currentId;
      });
      region("chatActions", {
        canAct: canAct,
        currentId: view.currentId,
        section: view.section,
        deleting: view.list.deleting,
      }, drawChatActions);
      region("chatDelete", {
        currentId: view.currentId,
        section: view.section,
        deleting: view.list.deleting,
      }, drawChatDelete);
      region("title", view.chatTitle, function (title) {
        els.chatTitle.textContent = title;
      });
      region("banner", view.banner, drawBanner);
      region("thread", view.thread, function (thread) {
        drawThread(thread, view.currentId);
      });
      region("composer", view.composer, drawComposer);
      applyWantedFocus();
    }
  }

  // Whether the explanation of the data was last made open (true) or closed (false) by default, null while
  // the panel is not shown.
  var explanationOpen = null;

  // (`panel`, not `view`: that name is the conversations' view, whose fields tests check by name.)
  function drawTelegram(panel) {
    if (panel === null || !panel.visible) {
      // Hidden means empty: what was shown (an account's name, a pairing code) is not left in the page.
      els.telegram.hidden = true;
      els.telegramText.textContent = "";
      els.telegramNotice.textContent = "";
      els.telegramPrivacy.textContent = "";
      els.telegramMore.hidden = true;
      els.telegramMore.open = false;
      explanationOpen = null;
      els.telegramOpen.removeAttribute("href");
      els.telegramOpen.hidden = true;
      return;
    }
    els.telegram.hidden = false;
    els.telegramText.textContent = panel.text;
    els.telegramNotice.hidden = !panel.notice;
    els.telegramNotice.textContent = panel.notice || "";
    // What connecting does with the person's words and photographs is kept in a <details> (it is long): it is
    // there whenever the logic has something to say about it. It is open for as long as the account is not
    // linked (that is when it is read, before pressing "connect") and closed once it is; the default is
    // set when that changes, so what the reader opens or closes themselves is left as it is.
    els.telegramPrivacy.textContent = panel.privacy;
    els.telegramMore.hidden = !panel.privacy;
    var openByDefault = panel.phase !== "linked";
    if (openByDefault !== explanationOpen) {
      els.telegramMore.open = openByDefault;
      explanationOpen = openByDefault;
    }
    // The one address this script ever sets on an element: the link to the bot that the server made, which
    // the logic has checked against a strict pattern (https://t.me/<bot>?start=<code>) before it gets here.
    els.telegramOpen.hidden = !panel.link;
    if (panel.link) {
      els.telegramOpen.setAttribute("href", panel.link);
    } else {
      els.telegramOpen.removeAttribute("href");
    }
    els.telegramConnect.hidden = !panel.canConnect;
    els.telegramConnect.textContent = panel.retry ? Logic.TEXT.retry : Logic.TELEGRAM_TEXT.connect;
    els.telegramDisconnect.hidden = !panel.canAskDisconnect;
    els.telegramConfirm.hidden = !panel.canConfirmDisconnect;
    els.telegramCancel.hidden = !panel.canCancel;
  }

  els.telegramConnect.addEventListener("click", function () {
    telegram.connect();
  });
  els.telegramDisconnect.addEventListener("click", function () {
    telegram.askDisconnect();
  });
  els.telegramConfirm.addEventListener("click", function () {
    telegram.disconnect();
  });
  els.telegramCancel.addEventListener("click", function () {
    telegram.cancelDisconnect();
  });

  // What the person's last action wants focused once the page has been drawn: "recipe" (the title of the
  // recipe pane, which is hidden until it is drawn), "keep" (the safe answer of the question "remove it?"),
  // "remove" (the button that asked the question), "chat-delete-cancel" (the safe answer of the question
  // "delete this conversation?"). The nodes are made by the drawing. It is asked of one drawing only: if
  // what it wanted is not there, nothing is focused, and nothing is focused later.
  var wantFocus = null;
  var focusNodes = {};

  function applyWantedFocus() {
    var wanted = wantFocus;
    wantFocus = null;
    if (wanted === "recipe") {
      if (!els.recipe.hidden) {
        els.recipeTitle.focus();
      }
    } else if (wanted !== null && focusNodes[wanted]) {
      focusNodes[wanted].focus();
    }
  }

  // The screens of the gate: waiting for the server (the words differ), and saying something could not be done.
  var WAITING = { loading: Logic.TEXT.loading, verifying: Logic.TEXT.verifying, signing_out: Logic.TEXT.signingOut };

  function drawScreens(data) {
    var screen = data.screen;
    drawn.section = null; // (the part in front is drawn again after every change of screen)
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
        drawn.chatActions = drawn.chatDelete = null;
        clear(els.messages);
        clear(els.list);
        clear(els.favoriteList);
        clear(els.recipeBody);
        clear(els.chatDelete);
        closeChatMenu(false);
        setHelp(false, false);
        els.chatActions.hidden = true;
        els.chatDelete.hidden = true;
        drawn.favoriteList = drawn.recipe = null;
        els.recipe.hidden = true;
        els.recipeTitle.textContent = "";
        els.favoritesStatus.textContent = "";
        els.favoritesRetry.hidden = true;
        lastRecipeId = null;
        wantFocus = null;
        focusNodes = {};
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

  // The actions of the open conversation ---------------------------------------------------------

  // What the menu offers for the conversation that is open. It is one list so that another
  // action is another entry here (with the confirmation it needs), not another control in the
  // header; the menu itself is built from it once, and an entry acts on the id it is given.
  function conversationActions() {
    return [
      {
        label: Logic.TEXT.deleteNamed,
        danger: true,
        run: function (id) {
          if (id === null || !lastView || lastView.currentId !== id) {
            return;
          }
          wantFocus = "chat-delete-cancel";
          controller.askDeleteConversation(id);
        },
      },
    ];
  }

  var conversationMenu = [];
  var menuOpen = false;
  var menuForId = null;

  function buildConversationMenu() {
    clear(els.chatActionsMenu);
    conversationMenu = conversationActions().map(function (entry) {
      var actionNode = h("button", {
        class: "chat-menu-item" + (entry.danger ? " chat-menu-item-danger" : ""),
        type: "button",
        role: "menuitem",
        tabindex: "-1",
        text: entry.label,
      });
      actionNode.addEventListener("click", function () {
        var id = lastView ? lastView.currentId : null;
        closeChatMenu(false);
        entry.run(id);
      });
      actionNode.addEventListener("keydown", onMenuItemKey);
      els.chatActionsMenu.appendChild(actionNode);
      return actionNode;
    });
  }

  function openChatMenu() {
    if (els.chatActions.hidden || els.chatActionsToggle.getAttribute("aria-disabled") === "true") {
      return;
    }
    menuOpen = true;
    menuForId = lastView ? lastView.currentId : null;
    els.chatActionsMenu.hidden = false;
    els.chatActionsToggle.setAttribute("aria-expanded", "true");
    if (conversationMenu.length) {
      conversationMenu[0].focus();
    }
  }

  // Closing normally gives the focus back to the button that asked (Escape, the button itself);
  // an outside click leaves the focus where the click put it, and Tab goes on from the menu.
  function closeChatMenu(restoreFocus) {
    menuOpen = false;
    menuForId = null;
    els.chatActionsMenu.hidden = true;
    els.chatActionsToggle.setAttribute("aria-expanded", "false");
    if (restoreFocus) {
      els.chatActionsToggle.focus();
    }
  }

  function within(node, ancestor) {
    for (var walk = node; walk; walk = walk.parentNode) {
      if (walk === ancestor) {
        return true;
      }
    }
    return false;
  }

  function onMenuItemKey(event) {
    var index = conversationMenu.indexOf(this);
    var count = conversationMenu.length;
    if (event.key === "Escape") {
      event.preventDefault();
      closeChatMenu(true);
      return;
    }
    if (event.key === "Tab") {
      closeChatMenu(false);
      return;
    }
    var next = null;
    if (event.key === "ArrowDown") {
      next = (index + 1) % count;
    } else if (event.key === "ArrowUp") {
      next = (index - 1 + count) % count;
    } else if (event.key === "Home") {
      next = 0;
    } else if (event.key === "End") {
      next = count - 1;
    }
    if (next !== null) {
      event.preventDefault();
      conversationMenu[next].focus();
    }
  }

  els.chatActionsToggle.addEventListener("click", function () {
    if (menuOpen) {
      closeChatMenu(true);
    } else {
      openChatMenu();
    }
  });

  els.chatActionsToggle.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!menuOpen) {
        openChatMenu();
      }
      if (menuOpen && conversationMenu.length) {
        conversationMenu[event.key === "ArrowUp" ? conversationMenu.length - 1 : 0].focus();
      }
    } else if (event.key === "Escape" && menuOpen) {
      event.preventDefault();
      closeChatMenu(true);
    }
  });

  // Clicking anything outside the menu and its button calls it off.
  document.addEventListener("click", function (event) {
    if (!menuOpen || within(event.target, els.chatActions)) {
      return;
    }
    closeChatMenu(false);
  });

  // Leaving the menu by keyboard (Tab, or the focus moving on its own) closes it and leaves the
  // focus where it went.
  els.chatActions.addEventListener("focusout", function (event) {
    if (menuOpen && !within(event.relatedTarget, els.chatActions)) {
      closeChatMenu(false);
    }
  });

  // The help explanation of the ideas -> chosen dish -> full recipe -> favorites workflow. It
  // is a disclosure beside the conversation title, so it is reachable while the conversation
  // is empty and while it is active; it only shows text and never sends, clears or changes
  // anything of the conversation.
  var helpOpen = false;
  function setHelp(open, focusToggle) {
    helpOpen = open;
    els.helpPanel.hidden = !open;
    els.helpToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (focusToggle) {
      els.helpToggle.focus();
    }
  }
  els.helpToggle.addEventListener("click", function () {
    setHelp(!helpOpen, false);
  });
  els.helpClose.addEventListener("click", function () {
    setHelp(false, true);
  });
  els.helpToggle.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && helpOpen) {
      event.preventDefault();
      setHelp(false, false);
    }
  });
  els.helpPanel.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && helpOpen) {
      event.preventDefault();
      setHelp(false, true);
    }
  });
  els.helpTitle.textContent = Logic.TEXT.helpTitle;
  els.helpText.textContent = Logic.TEXT.help;

  function drawChatActions(data) {
    var canAct = data.canAct && data.section === "chats";
    var busy = Boolean(data.deleting && data.deleting.id === data.currentId && data.deleting.phase === "deleting");
    els.chatActions.hidden = !canAct;
    els.chatActionsToggle.setAttribute("aria-disabled", busy ? "true" : "false");
    if (!canAct || busy || (menuOpen && menuForId !== data.currentId)) {
      closeChatMenu(false);
    }
  }

  // The question "delete this conversation?", under the header it was asked from. It is the same
  // confirmation the list used to hold. The safe answer (keep it) is the one focused when the
  // question appears; calling it off gives the focus back to the actions button. A failed
  // deletion stays in the question, with the reason and a retry. The list refresh, the return to
  // a new draft and the favorites are the logic's, here unchanged.
  function drawChatDelete(data) {
    var relevant = data.section === "chats" && data.currentId !== null && data.deleting && data.deleting.id === data.currentId;
    if (!relevant) {
      if (!els.chatDelete.hidden) {
        els.chatDelete.hidden = true;
        clear(els.chatDelete);
      }
      return;
    }
    els.chatDelete.hidden = false;
    clear(els.chatDelete);
    els.chatDelete.appendChild(deleteConversationControls(data.deleting));
  }

  function deleteConversationControls(deleting) {
    var box = h("div", { class: "confirm chat-delete-confirm", role: "group", "aria-label": Logic.TEXT.deleteAsk });
    if (deleting.phase === "deleting") {
      box.appendChild(
        h("p", { class: "confirm-text loading" }, [
          h("span", { class: "spinner", "aria-hidden": "true" }),
          h("span", { text: Logic.TEXT.deleting }),
        ])
      );
      return box;
    }
    var problem = deleting.phase === "error" ? deleting.error : null;
    box.appendChild(h("p", { class: "confirm-text", text: problem ? problem.text : Logic.TEXT.deleteAsk }));
    if (problem && problem.correlationId) {
      box.appendChild(
        h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", problem.correlationId) })
      );
    }
    var buttons = h("div", { class: "msg-actions" });
    var decide = action(problem ? Logic.TEXT.retry : Logic.TEXT.deleteConfirm, "btn-danger", function () {
      controller.deleteConversation().then(function (result) {
        if (!result || !result.ok) {
          return;
        }
        // The conversation that asked is gone: what was open is now a new empty draft (its
        // title, and the drawer that held the list closes to show it), or the list itself is
        // the thing to be back in.
        if (result.wasCurrent) {
          setDrawer(false, false);
          els.chatTitle.focus();
        } else {
          els.newConversation.focus();
        }
      });
    });
    buttons.appendChild(decide);
    var keep = action(Logic.TEXT.deleteCancel, "btn-quiet", function () {
      controller.cancelDeleteConversation();
      els.chatActionsToggle.focus();
    });
    focusNodes["chat-delete-cancel"] = keep;
    buttons.appendChild(keep);
    box.appendChild(buttons);
    if (problem) {
      // The confirmation changed into the failure with the answer under the cursor gone: the
      // retry takes the focus, and the reason is announced.
      focusNodes["chat-delete-retry"] = decide;
      wantFocus = "chat-delete-retry";
      alertText(problem.text);
    }
    return box;
  }

  // The favorites ------------------------------------------------------------------------------------------

  // The structured-recipe switch changes what keeps a recipe: one "Сохранить" per recipe
  // under an answer, not one whole-answer "В избранное". The empty hint must name the control
  // that is actually there.
  function favoritesEmptyText() {
    return structuredRecipes ? Logic.TEXT.favoritesEmptyStructured : Logic.TEXT.favoritesEmpty;
  }

  // Which part is in front: the conversations (the chat) or the favorites (the recipe).
  function drawSection(section) {
    var favorites = section === "favorites";
    els.tabChats.setAttribute("aria-pressed", favorites ? "false" : "true");
    els.tabFavorites.setAttribute("aria-pressed", favorites ? "true" : "false");
    els.panelChats.hidden = favorites;
    els.panelFavorites.hidden = !favorites;
    els.chat.hidden = favorites;
    els.recipe.hidden = !favorites;
  }

  function drawFavoriteList(list) {
    var status = "";
    els.favoritesRetry.hidden = true;
    if (list.status === "loading" && list.items.length === 0) {
      status = Logic.TEXT.favoritesLoading;
    } else if (list.status === "error") {
      status = list.error ? list.error.text : Logic.TEXT.favoritesFailed;
      els.favoritesRetry.hidden = false;
    } else if (list.status === "ready" && list.items.length === 0) {
      status = favoritesEmptyText();
    }
    els.favoritesStatus.textContent = status;
    clear(els.favoriteList);
    list.items.forEach(function (item) {
      var button = h("button", { class: "conv-item", type: "button", "aria-current": item.current ? "true" : false }, [
        h("span", { class: "conv-label", text: item.title }),
        h("span", { class: "conv-meta", text: item.meta }),
      ]);
      button.addEventListener("click", function () {
        openFavorite(item.id);
      });
      els.favoriteList.appendChild(h("li", null, [button]));
    });
  }

  var lastRecipeId = null;

  function drawRecipe(data) {
    focusNodes = {};
    var box = els.recipeBody;
    var current = data.current;
    var scrolled = current && lastRecipeId === current.id ? box.scrollTop : 0;
    clear(box);
    if (current === null) {
      lastRecipeId = null;
      els.recipeTitle.textContent = Logic.TEXT.favoritesTab;
      var empty = h("div", { class: "empty" }, [
        h("p", { class: "lead", text: data.status === "ready" && data.count === 0 ? favoritesEmptyText() : Logic.TEXT.favoritesPick }),
      ]);
      if (data.count > 0) {
        var choose = action("Выбрать рецепт", "btn-primary only-narrow", function () {
          setDrawer(true, false);
        });
        empty.appendChild(choose);
      }
      box.appendChild(empty);
      return;
    }
    lastRecipeId = current.id;
    els.recipeTitle.textContent = current.title || "Рецепт";
    if (current.status === "loading") {
      box.appendChild(
        h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode(Logic.TEXT.recipeLoading)])
      );
    } else if (current.status === "error" || current.status === "gone") {
      var problem = h("div", { class: "empty" }, [
        h("p", { class: "notice notice-error", text: current.error ? current.error.text : Logic.TEXT.recipeFailed }),
        current.error && current.error.correlationId
          ? h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", current.error.correlationId) })
          : null,
      ]);
      if (current.status === "error") {
        problem.appendChild(
          action(Logic.TEXT.retry, "btn-primary", function () {
            controller.reloadRecipe();
          })
        );
      }
      box.appendChild(problem);
      alertText(current.error ? current.error.text : "");
    } else {
      if (current.meta) {
        box.appendChild(h("p", { class: "hint recipe-meta", text: current.meta }));
      }
      if (current.editor) {
        box.appendChild(recipeEditor(current));
      } else {
        box.appendChild(recipeContent(current));
        box.appendChild(
          h("div", { class: "recipe-actions" }, [
            action(Logic.TEXT.editAction, "btn-secondary", function () {
              wantFocus = "edit-title";
              controller.beginEditFavorite();
            }),
          ])
        );
        box.appendChild(removalControls(current));
      }
    }
    box.scrollTop = scrolled;
  }

  // The card as it was stored: a structured favorite draws its fields (the text is the
  // same content rendered for older clients), a legacy one draws its text exactly as it
  // is. All of it goes into the page as text nodes.
  function recipeContent(current) {
    var wrapper = h("div", { class: "recipe-content" });
    if (current.kind === "structured") {
      if (current.servings) {
        wrapper.appendChild(
          h("p", { class: "recipe-servings" }, [
            h("span", { class: "visually-hidden", text: Logic.TEXT.servings + ": " }),
            document.createTextNode(current.servings),
          ])
        );
      }
      wrapper.appendChild(
        recipeSection(
          Logic.TEXT.ingredients,
          h(
            "ul",
            { class: "recipe-list" },
            current.ingredients.map(function (ingredient) {
              return h("li", { text: ingredient.name + " — " + ingredient.amount });
            })
          )
        )
      );
      wrapper.appendChild(
        recipeSection(
          Logic.TEXT.steps,
          h(
            "ol",
            { class: "recipe-list recipe-steps" },
            current.steps.map(function (step) {
              return h("li", { text: step });
            })
          )
        )
      );
    } else {
      var text = textBlocks(current.text);
      text.className += " recipe-text";
      wrapper.appendChild(text);
    }
    if (current.notes) {
      wrapper.appendChild(recipeSection(Logic.TEXT.notes, textBlocks(current.notes)));
    }
    return wrapper;
  }

  function recipeSection(title, body) {
    return h("section", { class: "recipe-section" }, [h("h2", { class: "recipe-heading", text: title }), body]);
  }

  // The editor. Nothing is sent until Save; every button collects the fields as they are
  // in the page first, so a change typed but not yet acted on is never lost by a redraw.
  function recipeEditor(current) {
    var editor = current.editor;
    var draft = editor.draft;
    var saving = editor.phase === "saving";
    var conflict = editor.phase === "conflict";
    var structured = current.kind === "structured";
    var ingredientInputs = [];
    var stepInputs = [];

    function field(labelText, input) {
      // The field is inside its label, so it is associated without an id to invent and
      // the label is a target a thumb can hit.
      return h("label", { class: "edit-field" }, [
        h("span", { class: "edit-label", text: labelText }),
        input,
      ]);
    }

    function textArea(value, rows) {
      var node = h("textarea", { rows: rows, readonly: saving, value: false });
      node.textContent = value;
      node.value = value;
      return node;
    }

    function collect() {
      return {
        title: titleInput.value,
        text: textField ? textField.value : draft.text,
        notes: notesField.value,
        servings: servingsInput ? servingsInput.value : draft.servings,
        ingredients: ingredientInputs.map(function (pair) {
          return { name: pair.name.value, amount: pair.amount.value };
        }),
        steps: stepInputs.map(function (input) {
          return input.value;
        }),
      };
    }

    // Typing is kept in the controller's draft as it happens, not only in the page: a
    // session recheck, a return to the tab, an online event or any other redrawing
    // rebuilds this form from the draft, and an edit that was typed but not submitted
    // must survive it. Nothing is redrawn here (that would take the caret away) and
    // nothing is sent.
    function keepDraft(node) {
      node.addEventListener("input", function () {
        controller.syncFavoriteDraft(collect());
      });
      return node;
    }

    function submitEdit() {
      if (saving) {
        return;
      }
      var saved = conflict
        ? controller.overwriteFavorite(collect())
        : controller.saveFavoriteEdit(collect());
      if (saved && saved.then) {
        saved.then(
          function (result) {
            if (result && result.ok) {
              els.recipeTitle.focus(); // (the form is gone; the card is back)
            }
          },
          function () {
            // (the controller reports a failed save in the editor; nothing to add here)
          }
        );
      }
    }

    function moveStep(index, delta) {
      var target = index + delta;
      if (saving || target < 0 || target >= stepInputs.length) {
        return;
      }
      var next = collect();
      var moved = next.steps.splice(index, 1)[0];
      next.steps.splice(target, 0, moved);
      controller.updateFavoriteDraft(next);
    }

    var form = h("form", { class: "recipe-editor", novalidate: true, "aria-label": "Редактирование рецепта" });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submitEdit();
    });

    var titleInput = keepDraft(h("input", {
      id: "recipe-edit-title",
      class: "edit-title",
      type: "text",
      value: draft.title,
      readonly: saving,
      autocomplete: "off",
    }));
    focusNodes["edit-title"] = titleInput;
    form.appendChild(field(Logic.TEXT.editTitleLabel, titleInput));

    var textField = null;
    var servingsInput = null;
    if (structured) {
      servingsInput = keepDraft(h("input", {
        class: "edit-servings",
        type: "text",
        value: draft.servings,
        readonly: saving,
        autocomplete: "off",
      }));
      form.appendChild(field(Logic.TEXT.editServingsLabel, servingsInput));

      var ingredients = h("div", { class: "edit-lines" });
      draft.ingredients.forEach(function (ingredient, index) {
        var name = keepDraft(h("input", {
          class: "ingredient-name",
          type: "text",
          value: ingredient.name,
          readonly: saving,
          autocomplete: "off",
          "aria-label": Logic.TEXT.editIngredientName + " " + (index + 1),
        }));
        var amount = keepDraft(h("input", {
          class: "ingredient-amount",
          type: "text",
          value: ingredient.amount,
          readonly: saving,
          autocomplete: "off",
          "aria-label": Logic.TEXT.editIngredientAmount + " " + (index + 1),
        }));
        ingredientInputs.push({ name: name, amount: amount });
        ingredients.appendChild(
          h("div", { class: "edit-row" }, [
            name,
            amount,
            action(
              "Удалить",
              "btn-quiet",
              function () {
                var next = collect();
                next.ingredients.splice(index, 1);
                controller.updateFavoriteDraft(next);
              },
              Logic.TEXT.editRemoveIngredient + " " + (index + 1)
            ),
          ])
        );
      });
      form.appendChild(
        h("section", { class: "edit-section" }, [
          h("h2", { class: "recipe-heading", text: Logic.TEXT.ingredients }),
          ingredients,
          action(Logic.TEXT.editAddIngredient, "btn-secondary", function () {
            if (saving) {
              return;
            }
            var next = collect();
            next.ingredients.push({ name: "", amount: "" });
            controller.updateFavoriteDraft(next);
          }),
        ])
      );

      var steps = h("div", { class: "edit-lines" });
      draft.steps.forEach(function (step, index) {
        // A step is one line by the protocol's rule (the server refuses control characters),
        // but a long instruction must be readable while it is edited: the field wraps it and
        // can be dragged taller. A line break typed or pasted becomes the space it stands for,
        // so what is sent is still text the existing validation can store.
        var input = textArea(step, 3);
        input.className = "edit-step";
        input.setAttribute("aria-label", Logic.TEXT.editStep + " " + (index + 1));
        input.addEventListener("keydown", function (event) {
          if (event.key === "Enter" && !event.isComposing) {
            event.preventDefault();
          }
        });
        input.addEventListener("input", function () {
          var folded = input.value.replace(/\r\n?|\n/g, " ");
          if (folded !== input.value) {
            input.value = folded;
          }
          controller.syncFavoriteDraft(collect());
        });
        stepInputs.push(input);
        steps.appendChild(
          h("div", { class: "edit-row edit-row-step" }, [
            input,
            action(
              Logic.TEXT.editMoveUp,
              "btn-quiet",
              function () {
                moveStep(index, -1);
              },
              Logic.TEXT.editMoveUp + " " + (index + 1)
            ),
            action(
              Logic.TEXT.editMoveDown,
              "btn-quiet",
              function () {
                moveStep(index, 1);
              },
              Logic.TEXT.editMoveDown + " " + (index + 1)
            ),
            action(
              "Удалить",
              "btn-quiet",
              function () {
                var next = collect();
                next.steps.splice(index, 1);
                controller.updateFavoriteDraft(next);
              },
              Logic.TEXT.editRemoveStep + " " + (index + 1)
            ),
          ])
        );
      });
      form.appendChild(
        h("section", { class: "edit-section" }, [
          h("h2", { class: "recipe-heading", text: Logic.TEXT.steps }),
          steps,
          action(Logic.TEXT.editAddStep, "btn-secondary", function () {
            if (saving) {
              return;
            }
            var next = collect();
            next.steps.push("");
            controller.updateFavoriteDraft(next);
          }),
        ])
      );
    } else {
      textField = keepDraft(textArea(draft.text, 10));
      textField.className = "edit-text";
      textField.setAttribute("aria-label", Logic.TEXT.editTextLabel);
      form.appendChild(field(Logic.TEXT.editTextLabel, textField));
    }

    var notesField = keepDraft(textArea(draft.notes, 3));
    notesField.className = "edit-notes";
    notesField.setAttribute("aria-label", Logic.TEXT.editNotesLabel);
    form.appendChild(field(Logic.TEXT.editNotesLabel, notesField));

    if (editor.error) {
      form.appendChild(
        h("p", { class: "notice notice-error", role: "alert", text: editor.error.text })
      );
      if (editor.error.correlationId) {
        form.appendChild(
          h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", editor.error.correlationId) })
        );
      }
    }

    var buttons = h("div", { class: "edit-actions" });
    if (conflict) {
      // A real submit button, so Enter in a field also sends the form.
      buttons.appendChild(
        h("button", {
          class: "btn btn-primary",
          type: "submit",
          text: Logic.TEXT.editOverwrite,
        })
      );
      buttons.appendChild(
        action(Logic.TEXT.editReload, "btn-secondary", function () {
          controller.reloadFavoriteAfterConflict();
        })
      );
    } else {
      buttons.appendChild(
        h("button", {
          class: "btn " + (saving ? "btn-quiet" : "btn-primary"),
          type: "submit",
          text: saving ? Logic.TEXT.editSaving : Logic.TEXT.editSave,
          "aria-disabled": saving ? "true" : false,
        })
      );
    }
    buttons.appendChild(
      action(Logic.TEXT.editCancel, "btn-quiet", function () {
        if (saving) {
          return;
        }
        controller.cancelEditFavorite();
      })
    );
    form.appendChild(buttons);
    return form;
  }

  // "Убрать из избранного", and after it the question, which is answered before anything is removed.
  function removalControls(current) {
    var removing = current.removing;
    var row = h("div", { class: "recipe-actions" });
    if (removing === null) {
      var remove = action(Logic.TEXT.removeAction, "btn-quiet", function () {
        wantFocus = "keep";
        controller.askRemoveFavorite(current.id);
      });
      focusNodes.remove = remove;
      row.appendChild(remove);
      return row;
    }
    if (removing.phase === "removing") {
      row.appendChild(
        h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode(Logic.TEXT.removing)])
      );
      return row;
    }
    var question = h("div", { class: "confirm", role: "group", "aria-label": Logic.TEXT.removeAsk });
    question.appendChild(h("p", { class: "confirm-text", text: removing.error ? removing.error.text : Logic.TEXT.removeAsk }));
    if (removing.error && removing.error.correlationId) {
      question.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", removing.error.correlationId) }));
    }
    var buttons = h("div", { class: "msg-actions" });
    buttons.appendChild(
      action(removing.error ? Logic.TEXT.retry : Logic.TEXT.removeConfirm, "btn-danger", function () {
        controller.removeFavorite();
      })
    );
    var keep = action(Logic.TEXT.removeKeep, "btn-quiet", function () {
      wantFocus = "remove";
      controller.cancelRemoveFavorite();
    });
    focusNodes.keep = keep;
    buttons.appendChild(keep);
    question.appendChild(buttons);
    row.appendChild(question);
    if (removing.error) {
      alertText(removing.error.text);
    }
    return row;
  }

  // Opening another card or leaving the page gives up an open editor: ask first, unless
  // the browser has nobody to ask (the call is best effort).
  function leaveEditor() {
    if (!controller.hasUnsavedChanges()) {
      return true;
    }
    if (typeof window.confirm !== "function") {
      return true;
    }
    return window.confirm(Logic.TEXT.editUnsaved);
  }

  function openFavorite(id) {
    if (!leaveEditor()) {
      return;
    }
    setDrawer(false, false);
    wantFocus = "recipe";
    controller.openFavorite(id);
  }

  // Under an answer of the assistant: keep it, or (when it is kept) say so and open it.
  function favoriteActions(mark, messageId, conversationId) {
    var row = h("div", { class: "msg-actions msg-favorite" });
    if (mark.state === "saved") {
      row.appendChild(h("span", { class: "fav-mark", text: Logic.TEXT.favoriteKept }));
      row.appendChild(
        action(Logic.TEXT.favoriteOpen, "btn-quiet", function () {
          openFavorite(mark.id);
        }, "Открыть сохраненный рецепт")
      );
      return row;
    }
    if (mark.state === "saving") {
      row.appendChild(
        h("p", { class: "msg-note" }, [h("span", { class: "spinner", "aria-hidden": "true" }), h("span", { text: Logic.TEXT.favoriteKeeping })])
      );
      return row;
    }
    if (mark.state === "error") {
      row.appendChild(h("p", { class: "msg-note msg-note-error", text: mark.error.text }));
    }
    row.appendChild(
      action(Logic.TEXT.favoriteKeep, "btn-quiet", function () {
        controller.keepFavorite(conversationId, messageId).then(function (result) {
          if (result && !result.ok && result.reason !== "busy" && result.reason !== "stale") {
            var failed = lastView && lastView.thread.items.filter(function (item) {
              return item.type === "message" && item.id === messageId && item.favorite && item.favorite.error;
            })[0];
            if (failed) {
              alertText(failed.favorite.error.text);
            }
          }
        });
      }, "Сохранить этот ответ в избранное")
    );
    return row;
  }

  // A recipe title on a control: long enough to know which recipe, short enough for a
  // phone. (The title itself may be up to 200 characters; it stays a text node.)
  function shortTitle(title) {
    var points = Array.from(String(title));
    return points.length <= 48 ? String(title) : points.slice(0, 48).join("") + "…";
  }

  // Under an answer that carries structured recipes: one independent Save control for
  // each, named by the recipe and identified by (message, position). One recipe's state
  // never changes another's.
  function recipeActions(recipe, messageId, conversationId) {
    var row = h("div", { class: "msg-actions msg-favorite" });
    var shown = shortTitle(recipe.title);
    var label = Logic.TEXT.saveRecipeNamed.replace("{title}", shown);
    if (recipe.state === "saved") {
      row.appendChild(
        h("span", { class: "fav-mark", text: shown + " — " + Logic.TEXT.recipeSaved })
      );
      row.appendChild(
        action(
          Logic.TEXT.openRecipe,
          "btn-quiet",
          function () {
            openFavorite(recipe.favoriteId);
          },
          "Открыть сохраненный рецепт «" + recipe.title + "»"
        )
      );
      return row;
    }
    if (recipe.state === "saving") {
      row.appendChild(
        h("p", { class: "msg-note" }, [
          h("span", { class: "spinner", "aria-hidden": "true" }),
          h("span", { text: shown + ": " + Logic.TEXT.recipeSaving }),
        ])
      );
      return row;
    }
    if (recipe.state === "error") {
      row.appendChild(
        h("p", { class: "msg-note msg-note-error", text: shown + ": " + recipe.error.text })
      );
    }
    row.appendChild(
      action(label, "btn-quiet", function () {
        controller.keepFavorite(conversationId, messageId, recipe.position).then(function (result) {
          if (result && !result.ok && result.reason !== "busy" && result.reason !== "stale") {
            var failed = lastView && lastView.thread.items.filter(function (item) {
              return item.type === "message" && item.id === messageId;
            })[0];
            var mark = failed && failed.recipes ? failed.recipes.filter(function (found) {
              return found.position === recipe.position;
            })[0] : null;
            if (mark && mark.error) {
              alertText(mark.error.text);
            }
          }
        });
      }, label)
    );
    return row;
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

  function action(label, className, handler, description) {
    var button = h("button", { class: "btn " + className, type: "button", text: label, "aria-label": description || false });
    button.addEventListener("click", handler);
    return button;
  }

  // A long answer -- a recipe, usually -- is a block of reading, not a short line: on a wide
  // window it takes the reading column instead of shrinking to its longest line. A short reply
  // keeps its bubble. A structured recipe (title, fields and steps) runs to several hundred
  // characters; a conversational reply is a sentence or two.
  var LONG_ANSWER_LENGTH = 200;

  function messageNode(item, conversationId) {
    var mine = item.role === "user";
    var meta = h("div", { class: "msg-meta" }, [
      item.time ? h("time", { datetime: item.iso || false, text: item.time }) : null,
    ]);
    var classes = "msg " + (mine ? "msg-user" : "msg-assistant") + (item.status === "failed" ? " msg-failed" : "");
    if (!mine && String(item.text || "").length >= LONG_ANSWER_LENGTH) {
      classes += " msg-wide";
    }
    var node = h("article", {
      class: classes,
      "data-id": item.type === "message" ? String(item.id) : false,
    });
    node.appendChild(h("p", { class: "visually-hidden", text: mine ? "Вы:" : "Помощник:" }));
    if (item.photo) {
      // The photo that came with the message. Its address is this site's own route, which the logic checked
      // against a strict pattern (Logic.PHOTO_URL); the server serves it to its owner only.
      var photo = h("img", { class: "msg-photo", src: item.photo.url, alt: "Фото к сообщению", loading: "lazy" });
      photo.addEventListener("load", keepEnd);
      photo.addEventListener("error", keepEnd);
      node.appendChild(photo);
    }
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
      if (item.recipes && item.recipes.length) {
        item.recipes.forEach(function (recipe) {
          node.appendChild(recipeActions(recipe, item.id, conversationId));
        });
      } else if (item.favorite) {
        node.appendChild(favoriteActions(item.favorite, item.id, conversationId));
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

  // A photograph has no height until it has loaded, so a thread that was scrolled to its newest message when
  // it was drawn is longer by the photographs a moment later, and the newest message is out of sight. While the
  // reader has not scrolled away from the end, the thread follows it.
  var followEnd = false;
  function keepEnd() {
    if (followEnd) {
      els.messages.scrollTop = els.messages.scrollHeight;
    }
  }
  els.messages.addEventListener("scroll", function () {
    followEnd = nearBottom();
  });

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
    followEnd = nearBottom();
    lastThread = { id: conversationId, first: firstId, last: lastKey };
  }

  // The composer -----------------------------------------------------------------------------------------

  function drawComposer(composer) {
    els.input.readOnly = composer.locked;
    els.send.setAttribute("aria-disabled", composer.locked ? "true" : "false");
    els.send.className = "btn btn-primary btn-send" + (composer.locked ? " is-disabled" : "");
    els.send.textContent = composer.creating ? "Создаем разговор…" : "Отправить";
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
    if (isOpen) {
      closeChatMenu(false); // (the drawer covers the header: its menu is not left open behind it)
    }
    els.app.setAttribute("data-drawer", isOpen ? "open" : "closed");
    els.drawerToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    els.backdrop.hidden = !isOpen;
    els.chat.inert = isOpen;
    if (isOpen) {
      (els.tabFavorites.getAttribute("aria-pressed") === "true" ? els.tabFavorites : els.tabChats).focus();
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
    closeChatMenu(false);
    controller.selectConversation(id);
    els.chatTitle.focus();
  }

  els.newConversation.addEventListener("click", function () {
    setDrawer(false, false);
    closeChatMenu(false);
    controller.startDraft();
    els.input.focus();
  });
  els.listRetry.addEventListener("click", function () {
    controller.reloadList();
  });
  els.tabChats.addEventListener("click", function () {
    controller.showSection("chats");
  });
  els.tabFavorites.addEventListener("click", function () {
    controller.showSection("favorites");
  });
  els.favoritesRetry.addEventListener("click", function () {
    controller.reloadFavorites();
  });
  els.bannerClose.addEventListener("click", function () {
    controller.dismissBanner();
  });

  els.logout.addEventListener("click", function () {
    if (els.logout.getAttribute("aria-disabled") === "true") {
      return;
    }
    if (!leaveEditor()) {
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
            els.emailSubmit.textContent = "Отправить ссылку еще раз";
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
  // A refresh or a closed tab cannot be stopped by the application: the browser's own
  // question is the best that can be done for an editor that was not saved.
  window.addEventListener("beforeunload", function (event) {
    if (controller.hasUnsavedChanges()) {
      event.preventDefault();
      event.returnValue = "";
    }
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
      telegram.recheck();
    }
  });
  document.addEventListener("resume", function () {
    controller.recheck();
  });

  buildConversationMenu();
  render();
  controller.start();
})();
