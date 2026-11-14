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
    helpBackdrop: byId("help-backdrop"),
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
    tabShopping: byId("tab-shopping"),
    profileOpen: byId("profile-open"),
    panelChats: byId("panel-chats"),
    panelFavorites: byId("panel-favorites"),
    panelShopping: byId("panel-shopping"),
    profile: byId("profile"),
    profileTitle: byId("profile-title"),
    profileBody: byId("profile-body"),
    newConversation: byId("new-conversation"),
    listStatus: byId("list-status"),
    listRetry: byId("list-retry"),
    list: byId("conversation-list"),
    favoritesStatus: byId("favorites-status"),
    favoritesRetry: byId("favorites-retry"),
    favoriteList: byId("favorite-list"),
    shoppingStatus: byId("shopping-status"),
    shoppingRetry: byId("shopping-retry"),
    shoppingList: byId("shopping-list"),
    shopping: byId("shopping"),
    shoppingTitle: byId("shopping-title"),
    shoppingBody: byId("shopping-body"),
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

  // The one thing the profile changes outside its own pane: the size of the whole signed-in
  // application. It is a rem base, so every text, control and gap grows together.
  function applyTextSize(size) {
    var root = document.documentElement || document.body;
    if (!root) {
      return;
    }
    if (size === "large") {
      root.setAttribute("data-text-size", "large");
    } else {
      root.removeAttribute("data-text-size");
    }
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
    // The profile's text size is applied to the signed-in application and nowhere else: the
    // attribute goes on <html> (every rem size is relative to it) and comes off on the way
    // to any other screen, so the sign-in and failure screens keep the browser's own size.
    region("textSize", view.screen === "app" ? view.profile.textSize : "normal", applyTextSize);
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
      region("profile", view.profile, drawProfile);
      // (The state of the list matters to the pane only while no recipe is open: it then says what to do.)
      var open = view.favorites.current !== null;
      region(
        "recipe",
        { current: view.favorites.current, status: open ? null : view.favorites.status, count: open ? null : view.favorites.items.length },
        drawRecipe
      );
      // The actions belong to an existing conversation that the list still holds (the logic refuses to
      // ask about any other), and only while the conversations are in front. The drawing of the recipe
      // resets focusNodes, so these -- and the shopping pane's own safe answers -- are drawn after it.
      region("shopping", view.shopping, drawShopping);
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
    } else if (wanted === "profile") {
      if (!els.profile.hidden) {
        els.profileTitle.focus();
      }
    } else if (wanted === "profile-name") {
      // The form «Изменить» draws: its name field is the first thing to work in.
      if (!els.profile.hidden) {
        var nameInput = findByFocus(els.profileBody, "profile-name");
        if (nameInput) {
          nameInput.focus();
        }
      }
    } else if (wanted !== null && wanted.indexOf("profile-measure-") === 0) {
      // A refused measure: the redrawn form puts the keyboard on the field to fix.
      if (!els.profile.hidden) {
        var measureInput = findByFocus(els.profileBody, wanted);
        if (measureInput) {
          measureInput.focus();
        }
      }
    } else if (wanted !== null && wanted.indexOf("source-glass-") === 0) {
      // A source glass chosen or repeated: the redrawn question keeps the keyboard on it.
      if (!els.recipe.hidden) {
        var glassInput = findByFocus(els.recipeBody, wanted);
        if (glassInput) {
          glassInput.focus();
        }
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
    els.shopping.hidden = screen !== "app";
    els.profile.hidden = screen !== "app";
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
        clear(els.shoppingList);
        clear(els.shoppingBody);
        clear(els.profileBody);
        clear(els.chatDelete);
        closeChatMenu(false);
        setHelp(false, false);
        els.chatActions.hidden = true;
        els.chatDelete.hidden = true;
        drawn.favoriteList = drawn.recipe = drawn.shopping = null;
        els.recipe.hidden = true;
        els.recipeTitle.textContent = "";
        els.favoritesStatus.textContent = "";
        els.favoritesRetry.hidden = true;
        els.shopping.hidden = true;
        els.shoppingTitle.textContent = "";
        els.shoppingStatus.textContent = "";
        els.shoppingRetry.hidden = true;
        els.profileTitle.textContent = "";
        drawn.profile = null;
        lastRecipeId = null;
        lastShoppingKey = null;
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
  // is a modal dialog over the page, opened from the conversation header: the page behind it
  // is inert while it is up, the focus stays inside it, and closing returns the focus to the
  // button that asked. It only shows text and never sends, clears or changes anything of the
  // conversation; opening and closing it leave the messages, the scroll position and the
  // unsent draft exactly as they were.
  var helpOpen = false;

  // The controls of the dialog, in reading order; the close button is the only one today,
  // but the trap does not depend on that.
  function helpControls() {
    var found = [];
    (function walk(node) {
      for (var i = 0; i < node.childNodes.length; i++) {
        var child = node.childNodes[i];
        if (child.nodeType !== 1) {
          continue;
        }
        if (child.localName === "button" && !child.disabled) {
          found.push(child);
        }
        walk(child);
      }
    })(els.helpPanel);
    return found;
  }

  function setHelp(open, focusToggle) {
    helpOpen = open;
    els.helpPanel.hidden = !open;
    els.helpBackdrop.hidden = !open;
    // While the dialog is up nothing behind it takes a click, a tap or a focus.
    els.app.inert = open;
    els.helpToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      els.helpClose.focus();
    } else if (focusToggle) {
      els.helpToggle.focus();
    }
  }

  els.helpToggle.addEventListener("click", function () {
    setHelp(!helpOpen, false);
  });
  els.helpClose.addEventListener("click", function () {
    setHelp(false, true);
  });
  els.helpBackdrop.addEventListener("click", function () {
    setHelp(false, true);
  });
  // The keys are heard on the document, not only on the dialog: the focus can be thrown out
  // of an open dialog (a script, an extension, a browser quirk), and Escape or Tab from the
  // page body must still close it or bring the focus back in. One listener is added when the
  // page starts and lives as long as the page; while the dialog is closed it does nothing at
  // all, so repeated opening and closing never stacks another handler.
  document.addEventListener("keydown", function (event) {
    if (!helpOpen) {
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setHelp(false, true);
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    // The focus never leaves the dialog: Tab on the last control wraps to the first, and
    // Shift+Tab on the first wraps to the last (with one control, both keep it there). A key
    // that arrives while the focus is outside the dialog pulls it back to the edge it came
    // from: the first control for Tab, the last for Shift+Tab.
    var controls = helpControls();
    if (!controls.length) {
      event.preventDefault();
      return;
    }
    var first = controls[0];
    var last = controls[controls.length - 1];
    var inside = within(document.activeElement, els.helpPanel);
    if (event.shiftKey) {
      if (!inside || document.activeElement === first) {
        event.preventDefault();
        last.focus();
      }
    } else if (!inside || document.activeElement === last) {
      event.preventDefault();
      first.focus();
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

  // Which part is in front: the conversations (the chat), the favorites (the recipe) or the
  // shopping lists (their own pane, which holds a draft or a saved list).
  function drawSection(section) {
    var chats = section === "chats";
    var favorites = section === "favorites";
    var shopping = section === "shopping";
    var profile = section === "profile";
    els.tabChats.setAttribute("aria-pressed", chats ? "true" : "false");
    els.tabFavorites.setAttribute("aria-pressed", favorites ? "true" : "false");
    els.tabShopping.setAttribute("aria-pressed", shopping ? "true" : "false");
    els.profileOpen.setAttribute("aria-pressed", profile ? "true" : "false");
    els.panelChats.hidden = !chats;
    els.panelFavorites.hidden = !favorites;
    els.panelShopping.hidden = !shopping;
    els.chat.hidden = !chats;
    els.recipe.hidden = !favorites;
    els.shopping.hidden = !shopping;
    els.profile.hidden = !profile;
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

  // The shopping lists ------------------------------------------------------------------------------------

  var lastShoppingKey = null;
  var lastShoppingAlert = null;

  // A field of text in the shopping pane, associated with its label without an id per row.
  function shopField(labelText, input) {
    return h("label", { class: "shop-field" }, [
      h("span", { class: "edit-label", text: labelText }),
      input,
    ]);
  }

  function filledText(template, values) {
    return String(template).replace(/\{(\w+)\}/g, function (whole, name) {
      return Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : whole;
    });
  }

  function findByFocus(root, wanted) {
    var found = null;
    (function walk(node) {
      for (var i = 0; i < node.childNodes.length; i++) {
        var child = node.childNodes[i];
        if (child.nodeType !== 1) {
          continue;
        }
        if (child.getAttribute("data-focus") === wanted) {
          found = child;
        }
        walk(child);
      }
    })(root);
    return found;
  }

  function stateWord(stateName) {
    return stateName === "at_home" ? Logic.TEXT.atHome : stateName === "purchased" ? Logic.TEXT.bought : Logic.TEXT.toBuy;
  }

  function drawShopping(shopping) {
    // The side panel: the saved lists, active first, each with its progress and status.
    var status = "";
    els.shoppingRetry.hidden = true;
    if (shopping.status === "loading" && shopping.lists.length === 0) {
      status = Logic.TEXT.shoppingLoading;
    } else if (shopping.status === "error") {
      status = shopping.error ? shopping.error.text : Logic.TEXT.shoppingFailed;
      els.shoppingRetry.hidden = false;
    } else if (shopping.empty) {
      status = Logic.TEXT.shoppingEmpty;
    }
    els.shoppingStatus.textContent = status;
    clear(els.shoppingList);
    shopping.lists.forEach(function (entry) {
      var button = h(
        "button",
        {
          class: "conv-item",
          type: "button",
          "aria-current": entry.current ? "true" : false,
          disabled: shopping.blocked.indexOf(entry.id) !== -1,
        },
        [h("span", { class: "conv-label", text: entry.title }), h("span", { class: "conv-meta", text: entry.meta })]
      );
      button.addEventListener("click", function () {
        openShoppingList(entry.id);
      });
      els.shoppingList.appendChild(h("li", null, [button]));
    });

    els.shoppingTitle.textContent = shopping.title;
    var key = shopping.draft !== null ? "draft" : shopping.current !== null ? "list:" + shopping.current.id : "list";
    var focused =
      within(document.activeElement, els.shoppingBody) && document.activeElement
        ? document.activeElement.getAttribute("data-focus")
        : null;
    var scroll = lastShoppingKey === key ? els.shoppingBody.scrollTop : 0;
    clear(els.shoppingBody);
    if (shopping.draft !== null) {
      drawShoppingDraft(shopping.draft);
    } else if (shopping.current !== null) {
      drawShoppingList(shopping.current, shopping.notice);
    } else {
      drawShoppingEmpty(shopping);
    }
    lastShoppingKey = key;
    els.shoppingBody.scrollTop = scroll;
    if (focused) {
      var again = findByFocus(els.shoppingBody, focused);
      if (again) {
        again.focus();
      }
    }
  }

  function drawShoppingEmpty(shopping) {
    var box = h("div", { class: "shopping-content empty" });
    if (shopping.notice) {
      box.appendChild(drawShoppingNotice(shopping.notice));
    }
    if (shopping.status === "loading" && shopping.lists.length === 0) {
      box.appendChild(
        h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode(Logic.TEXT.shoppingLoading)])
      );
      els.shoppingBody.appendChild(box);
      return;
    }
    if (shopping.status === "error") {
      box.appendChild(
        h("p", { class: "notice notice-error", role: "alert", text: shopping.error ? shopping.error.text : Logic.TEXT.shoppingFailed })
      );
      if (shopping.error && shopping.error.correlationId) {
        box.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", shopping.error.correlationId) }));
      }
      els.shoppingBody.appendChild(box);
      return;
    }
    box.appendChild(
      h("p", { class: "lead", text: shopping.empty ? Logic.TEXT.shoppingEmpty : Logic.TEXT.shoppingPick })
    );
    els.shoppingBody.appendChild(box);
  }

  function drawShoppingNotice(notice) {
    return h("p", {
      class: "notice" + (notice.kind === "error" ? " notice-error" : ""),
      role: notice.kind === "error" ? "alert" : "status",
      text: notice.text,
    });
  }

  function draftCheckbox(ingredient, editable) {
    var box = h("input", {
      type: "checkbox",
      checked: ingredient.atHome,
      disabled: !editable,
      "aria-label": Logic.TEXT.atHome + ": " + ingredient.name,
      "data-focus": "draft-" + ingredient.position,
    });
    box.addEventListener("change", function () {
      controller.toggleShoppingDraftAtHome(ingredient.position, box.checked);
    });
    return h("label", { class: "shop-check" }, [
      box,
      h("span", { class: "shop-check-text" }, [
        h("span", { class: "shop-name", text: ingredient.name }),
        ingredient.amount ? h("span", { class: "shop-amount", text: " — " + ingredient.amount }) : null,
      ]),
    ]);
  }

  function drawShoppingDraft(draft) {
    var box = h("div", { class: "shopping-content" });
    els.shoppingBody.appendChild(box);
    if (draft.phase === "loading") {
      box.appendChild(
        h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode(Logic.TEXT.shoppingDraftLoading)])
      );
      box.appendChild(h("div", { class: "shop-actions" }, [action(Logic.TEXT.shoppingCancel, "btn-quiet", cancelDraft)]));
      return;
    }
    if (draft.phase === "load_error" || draft.phase === "load_gone") {
      box.appendChild(
        h("p", { class: "notice notice-error", role: "alert", text: draft.error ? draft.error.text : Logic.TEXT.shoppingDraftFailed })
      );
      if (draft.error && draft.error.correlationId) {
        box.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", draft.error.correlationId) }));
      }
      var loadActions = h("div", { class: "shop-actions" });
      if (draft.canRetry) {
        loadActions.appendChild(
          action(draft.retryLabel, "btn-primary", function () {
            controller.reloadShoppingDraft();
          })
        );
      }
      loadActions.appendChild(action(Logic.TEXT.shoppingCancel, "btn-quiet", cancelDraft));
      box.appendChild(loadActions);
      return;
    }

    if (draft.title) {
      box.appendChild(h("h2", { class: "shop-recipe-title", text: draft.title }));
    }
    if (draft.servings) {
      box.appendChild(
        h("p", { class: "recipe-servings" }, [
          h("span", { class: "visually-hidden", text: Logic.TEXT.servings + ": " }),
          document.createTextNode(draft.servings),
        ])
      );
    }
    box.appendChild(h("p", { class: "hint", text: draft.instruction }));
    box.appendChild(
      h("p", {
        class: "shop-remaining",
        role: "status",
        "aria-live": "polite",
        text: filledText(Logic.TEXT.shoppingRemaining, { count: draft.remaining }),
      })
    );
    box.appendChild(h("p", { class: "hint shop-unchecked-note", text: Logic.TEXT.shoppingUncheckedNote }));
    var draftItems = h("div", { class: "shop-draft-items" });
    draft.ingredients.forEach(function (ingredient) {
      draftItems.appendChild(draftCheckbox(ingredient, draft.editable));
    });
    box.appendChild(draftItems);

    if (draft.error) {
      box.appendChild(h("p", { class: "notice notice-error", role: "alert", text: draft.error.text }));
      if (draft.error.correlationId) {
        box.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", draft.error.correlationId) }));
      }
    }

    var buttons = h("div", { class: "shop-actions" });
    if (draft.canSave || draft.saving) {
      if (draft.saving) {
        buttons.appendChild(
          h("button", {
            class: "btn btn-quiet",
            type: "button",
            text: Logic.TEXT.shoppingSaving,
            "aria-disabled": "true",
            "data-focus": "draft-save",
          })
        );
      } else {
        var saveButton = action(Logic.TEXT.shoppingSave, "btn-primary", saveDraft, Logic.TEXT.shoppingSave);
        saveButton.setAttribute("data-focus", "draft-save");
        buttons.appendChild(saveButton);
      }
    } else if (draft.canRetry) {
      buttons.appendChild(
        action(draft.retryLabel, "btn-primary", function () {
          controller.retryShoppingDraft();
        }, false)
      );
    } else if (draft.canSaveNew) {
      buttons.appendChild(
        action(draft.saveNewLabel, "btn-primary", function () {
          controller.saveShoppingDraftWithNewKey();
        })
      );
    }
    if (draft.canCancel) {
      buttons.appendChild(
        action(draft.terminal ? Logic.TEXT.shoppingUnderstand : Logic.TEXT.shoppingCancel, "btn-quiet", cancelDraft)
      );
    }
    box.appendChild(buttons);
  }

  function cancelDraft() {
    var result = controller.cancelShoppingDraft();
    if (!result || !result.ok) {
      return;
    }
    if (result.origin === "favorites") {
      els.recipeTitle.focus();
    } else {
      els.chatTitle.focus();
    }
  }

  function saveDraft() {
    controller.saveShoppingDraft().then(function (result) {
      if (result && result.ok) {
        els.shoppingTitle.focus();
      }
    });
  }

  // The main interaction of a saved item: one checkbox that marks it bought (or unmarks it).
  // While any write of this list is unsettled (here or on another item) every conflicting
  // control is disabled: the controller would refuse the same overlap.
  function shoppingItemCheckbox(entry, locked) {
    var box = h("input", {
      type: "checkbox",
      checked: entry.state === "purchased",
      disabled: locked || Boolean(entry.pending),
      "aria-label":
        entry.state === "purchased" ? "Вернуть в список: " + entry.name : Logic.TEXT.toBuy + ": " + entry.name,
      "data-focus": "item-check-" + entry.id,
    });
    box.addEventListener("change", function () {
      controller.setShoppingItemState(entry.id, box.checked ? "purchased" : "unchecked");
    });
    return box;
  }

  function shoppingStateSelect(entry, locked) {
    var select = h("select", {
      class: "shop-state",
      disabled: locked || Boolean(entry.pending),
      "aria-label": Logic.TEXT.itemStateLabel + ": " + entry.name,
      "data-focus": "item-state-" + entry.id,
    });
    ["unchecked", "at_home", "purchased"].forEach(function (stateName) {
      select.appendChild(h("option", { value: stateName, text: stateWord(stateName) }));
    });
    select.value = entry.state;
    select.addEventListener("change", function () {
      controller.setShoppingItemState(entry.id, select.value);
    });
    return select;
  }

  function drawShoppingItem(entry, detail) {
    var readOnly = detail.readOnly;
    var locked = readOnly || detail.busy;
    var row = h("li", { class: "shop-item", "data-id": entry.id }, [
      h("div", { class: "shop-item-main" }, [
        h("label", { class: "shop-check" }, [
          shoppingItemCheckbox(entry, locked),
          h("span", { class: "shop-check-text" }, [
            h("span", { class: "shop-name", text: entry.name }),
            entry.amount ? h("span", { class: "shop-amount", text: " — " + entry.amount }) : null,
          ]),
        ]),
      ]),
    ]);
    var controls = h("div", { class: "shop-item-controls" });
    controls.appendChild(shoppingStateSelect(entry, locked));
    var inlineBusy =
      detail.editor !== null || detail.adding !== null || detail.removing !== null || detail.deleting !== null || detail.busy;
    if (!readOnly && !inlineBusy) {
      var editButton = action(Logic.TEXT.itemEdit, "btn-quiet", function () {
        wantFocus = "item-name-" + entry.id;
        controller.startShoppingItemEdit(entry.id);
      }, Logic.TEXT.itemEdit + ": " + entry.name);
      editButton.setAttribute("data-focus", "item-edit-" + entry.id);
      controls.appendChild(editButton);
      var deleteButton = action(Logic.TEXT.itemRemove, "btn-quiet", function () {
        wantFocus = "shop-keep-" + entry.id;
        controller.askDeleteShoppingItem(entry.id);
      }, Logic.TEXT.itemRemove + ": " + entry.name);
      deleteButton.setAttribute("data-focus", "item-delete-" + entry.id);
      focusNodes["item-delete-" + entry.id] = deleteButton;
      controls.appendChild(deleteButton);
    }
    row.appendChild(controls);
    if (detail.editor !== null && detail.editor.id === entry.id) {
      row.appendChild(shoppingItemEditor(detail.editor, detail.busy));
    }
    if (detail.removing !== null && detail.removing.id === entry.id) {
      row.appendChild(shoppingItemRemoval(detail.removing, entry, detail.busy));
    }
    return row;
  }

  function shoppingItemEditor(editor, busy) {
    var saving = editor.phase === "saving";
    var locked = saving || busy;
    var form = h("form", { class: "shop-item-form", novalidate: true, "aria-label": Logic.TEXT.itemEdit + ": " + editor.draft.name });
    var name = h("input", {
      id: "shop-edit-name-" + editor.id,
      type: "text",
      value: editor.draft.name,
      readonly: saving,
      autocomplete: "off",
      maxlength: "200",
      "data-focus": "item-name-" + editor.id,
    });
    var amount = h("input", {
      id: "shop-edit-amount-" + editor.id,
      type: "text",
      value: editor.draft.amount,
      readonly: saving,
      autocomplete: "off",
      maxlength: "100",
      "data-focus": "item-amount-" + editor.id,
    });
    function collect() {
      return { name: name.value, amount: amount.value };
    }
    name.addEventListener("input", function () {
      controller.syncShoppingItemEdit(collect());
    });
    amount.addEventListener("input", function () {
      controller.syncShoppingItemEdit(collect());
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!locked) {
        controller.saveShoppingItemEdit(collect()).then(function (result) {
          if (result && result.ok) {
            var node = findByFocus(els.shoppingBody, "item-check-" + result.id);
            if (node) {
              node.focus();
            }
          }
        });
      }
    });
    form.appendChild(
      h("label", { class: "shop-field", for: name.id }, [
        h("span", { class: "edit-label", text: Logic.TEXT.itemNameLabel }),
        name,
      ])
    );
    form.appendChild(
      h("label", { class: "shop-field", for: amount.id }, [
        h("span", { class: "edit-label", text: Logic.TEXT.itemAmountLabel }),
        amount,
      ])
    );
    if (editor.error) {
      form.appendChild(h("p", { class: "notice notice-error", role: "alert", text: editor.error.text }));
    }
    var buttons = h("div", { class: "shop-actions" });
    buttons.appendChild(
      h("button", {
        class: "btn " + (saving ? "btn-quiet" : "btn-primary"),
        type: "submit",
        text: saving ? Logic.TEXT.shoppingSaving : Logic.TEXT.itemSave,
        disabled: locked,
        "aria-disabled": locked ? "true" : false,
      })
    );
    buttons.appendChild(
      action(Logic.TEXT.itemEditCancel, "btn-quiet", function () {
        if (!saving) {
          controller.cancelShoppingItemEdit();
        }
      })
    );
    form.appendChild(buttons);
    return form;
  }

  function shoppingItemRemoval(removing, entry, busy) {
    var box = h("div", { class: "confirm", role: "group", "aria-label": Logic.TEXT.itemRemoveAsk });
    box.appendChild(
      h("p", { class: "confirm-text", text: removing.error ? removing.error.text : Logic.TEXT.itemRemoveAsk })
    );
    if (removing.error && removing.error.correlationId) {
      box.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", removing.error.correlationId) }));
    }
    var buttons = h("div", { class: "msg-actions" });
    if (removing.phase === "deleting") {
      buttons.appendChild(
        h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode(Logic.TEXT.itemRemoving)])
      );
    } else {
      var removeConfirm = action(removing.error ? Logic.TEXT.retry : Logic.TEXT.itemRemoveConfirm, "btn-danger", function () {
        controller.deleteShoppingItem().then(function (result) {
          if (result && result.ok) {
            var node = findByFocus(els.shoppingBody, "shop-add");
            if (node) {
              node.focus();
            }
          }
        });
      }, Logic.TEXT.itemRemove + ": " + entry.name);
      if (busy) {
        removeConfirm.setAttribute("disabled", "");
      }
      buttons.appendChild(removeConfirm);
      var keep = action(Logic.TEXT.itemRemoveKeep, "btn-quiet", function () {
        wantFocus = "item-delete-" + entry.id;
        controller.cancelDeleteShoppingItem();
      });
      focusNodes["shop-keep-" + entry.id] = keep;
      buttons.appendChild(keep);
    }
    box.appendChild(buttons);
    if (removing.error) {
      alertText(removing.error.text);
    }
    return box;
  }

  function drawShoppingList(detail, notice) {
    var box = h("div", { class: "shopping-content" });
    els.shoppingBody.appendChild(box);
    if (notice) {
      box.appendChild(drawShoppingNotice(notice));
    }
    if (detail.status === "loading") {
      box.appendChild(
        h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode(Logic.TEXT.shoppingListLoading)])
      );
      return;
    }
    if (detail.status === "error" || detail.status === "gone") {
      box.appendChild(
        h("p", { class: "notice notice-error", role: "alert", text: detail.error ? detail.error.text : Logic.TEXT.shoppingListFailed })
      );
      if (detail.error && detail.error.correlationId) {
        box.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", detail.error.correlationId) }));
      }
      var loadActions = h("div", { class: "shop-actions" });
      if (detail.status === "error") {
        loadActions.appendChild(
          action(Logic.TEXT.retry, "btn-primary", function () {
            controller.reloadShoppingList();
          }, false)
        );
      }
      loadActions.appendChild(action("Назад к покупкам", "btn-quiet", function () {
        controller.closeShoppingList();
        els.shoppingTitle.focus();
      }));
      box.appendChild(loadActions);
      if (detail.status === "error") {
        alertText(detail.error ? detail.error.text : "");
      }
      return;
    }

    if (detail.meta) {
      box.appendChild(h("p", { class: "hint shop-meta", text: detail.meta }));
    }
    box.appendChild(h("p", { class: "shop-progress", text: detail.progress }));
    var counts = h("ul", { class: "shop-counts", "aria-label": "Пункты списка" });
    [
      [Logic.TEXT.countToBuy, detail.counts.unchecked],
      [Logic.TEXT.countBought, detail.counts.purchased],
      [Logic.TEXT.countAtHome, detail.counts.at_home],
    ].forEach(function (pair) {
      counts.appendChild(
        h("li", { class: "shop-count" }, [
          h("span", { text: filledText(pair[0], { count: pair[1] }) }),
        ])
      );
    });
    box.appendChild(counts);
    box.appendChild(h("p", { class: "hint shop-unchecked-note", text: Logic.TEXT.shoppingUncheckedNote }));

    var actions = h("div", { class: "shop-actions" });
    var statusButton;
    if (detail.readOnly) {
      statusButton = action(Logic.TEXT.reopenList, "btn-secondary", function () {
        controller.setShoppingListStatus("active");
      });
      box.appendChild(h("p", { class: "hint shop-completed-note", text: Logic.TEXT.listCompletedNote }));
    } else {
      statusButton = action(Logic.TEXT.completeList, "btn-secondary", function () {
        controller.setShoppingListStatus("completed");
      });
    }
    statusButton.setAttribute("data-focus", "shop-status");
    if (detail.busy || detail.statusBusy) {
      statusButton.setAttribute("disabled", "");
    }
    actions.appendChild(statusButton);
    if (detail.statusBusy) {
      actions.appendChild(
        h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode(Logic.TEXT.shoppingSaving)])
      );
    }
    var deleteListButton = action(Logic.TEXT.deleteList, "btn-quiet", function () {
      wantFocus = "shop-list-keep";
      controller.askDeleteShoppingList();
    });
    deleteListButton.setAttribute("data-focus", "shop-list-delete");
    if (detail.busy) {
      deleteListButton.setAttribute("disabled", "");
    }
    focusNodes["shop-list-delete"] = deleteListButton;
    actions.appendChild(deleteListButton);
    box.appendChild(actions);

    if (detail.deleting !== null) {
      box.appendChild(shoppingListRemoval(detail));
    }
    if (detail.actionError) {
      box.appendChild(
        h("p", { class: "notice notice-error", role: "alert", text: detail.actionError.text })
      );
      if (detail.actionError.correlationId) {
        box.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", detail.actionError.correlationId) }));
      }
      if (lastShoppingAlert !== detail.actionError.text) {
        lastShoppingAlert = detail.actionError.text;
        alertText(detail.actionError.text);
      }
    } else {
      lastShoppingAlert = null;
    }

    if (detail.itemCount === 0) {
      box.appendChild(h("p", { class: "hint", text: Logic.TEXT.itemsEmpty }));
    }
    detail.groups.forEach(function (group) {
      var section = h("section", { class: "shop-group" }, [
        h("h2", { class: "shop-group-title", text: group.title + " (" + group.count + ")" }),
      ]);
      var groupItems = h("ul", { class: "shop-items" });
      group.items.forEach(function (item) {
        groupItems.appendChild(drawShoppingItem(item, detail));
      });
      section.appendChild(groupItems);
      box.appendChild(section);
    });

    if (detail.adding !== null) {
      // An unresolved addition is shown even over a list that has become completed since:
      // hiding it would leave the person with a warning, a retry and a cancellation they
      // cannot see, while the list refuses «Вернуть в работу» in the name of that same
      // hidden form. The form keeps what was typed; a completed list only refuses to send.
      box.appendChild(shoppingItemAdd(detail.adding, detail.busy, detail.readOnly));
    } else if (!detail.readOnly) {
      if (detail.editor === null && detail.removing === null && detail.deleting === null && !detail.busy) {
        var addButton = action(Logic.TEXT.addItem, "btn-secondary shop-add", function () {
          wantFocus = "shop-add-name";
          controller.beginShoppingItemAdd();
        });
        addButton.setAttribute("data-focus", "shop-add");
        box.appendChild(addButton);
      }
    }
  }

  function shoppingListRemoval(detail) {
    var deleting = detail.deleting;
    var box = h("div", { class: "confirm", role: "group", "aria-label": Logic.TEXT.deleteList });
    box.appendChild(
      h("p", {
        class: "confirm-text",
        text: deleting.error
          ? deleting.error.text
          : filledText(Logic.TEXT.deleteListAsk, { title: detail.title }),
      })
    );
    if (deleting.error && deleting.error.correlationId) {
      box.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", deleting.error.correlationId) }));
    }
    var buttons = h("div", { class: "msg-actions" });
    if (deleting.phase === "deleting") {
      buttons.appendChild(
        h("p", { class: "loading" }, [h("span", { class: "spinner", "aria-hidden": "true" }), document.createTextNode(Logic.TEXT.deletingList)])
      );
    } else {
      var deleteConfirm = action(deleting.error ? Logic.TEXT.retry : Logic.TEXT.deleteListConfirm, "btn-danger", function () {
        controller.deleteShoppingList().then(function (result) {
          if (result && result.ok) {
            els.shoppingTitle.focus();
          }
        });
      });
      if (detail.busy) {
        deleteConfirm.setAttribute("disabled", "");
      }
      buttons.appendChild(deleteConfirm);
      var keep = action(Logic.TEXT.deleteListKeep, "btn-quiet", function () {
        wantFocus = "shop-list-delete";
        controller.cancelDeleteShoppingList();
      });
      focusNodes["shop-list-keep"] = keep;
      buttons.appendChild(keep);
    }
    box.appendChild(buttons);
    if (deleting.error) {
      alertText(deleting.error.text);
    }
    return box;
  }

  function shoppingItemAdd(adding, busy, readOnly) {
    var saving = adding.phase === "saving";
    // A completed list is read-only: an unresolved addition keeps its form and its values,
    // and «Отмена» works, but the fields and «Добавить» wait for «Вернуть в работу».
    var completedList = readOnly === true;
    var locked = saving || busy;
    var form = h("form", { class: "shop-item-form shop-add", novalidate: true, "aria-label": Logic.TEXT.addItem });
    var name = h("input", {
      id: "shop-add-name",
      type: "text",
      value: adding.draft.name,
      readonly: saving || completedList,
      autocomplete: "off",
      maxlength: "200",
      "data-focus": "shop-add-name",
    });
    var amount = h("input", {
      id: "shop-add-amount",
      type: "text",
      value: adding.draft.amount,
      readonly: saving || completedList,
      autocomplete: "off",
      maxlength: "100",
      "data-focus": "shop-add-amount",
    });
    function collect() {
      return { name: name.value, amount: amount.value };
    }
    name.addEventListener("input", function () {
      controller.syncShoppingItemAdd(collect());
    });
    amount.addEventListener("input", function () {
      controller.syncShoppingItemAdd(collect());
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!locked && !completedList) {
        controller.saveShoppingItemAdd(collect()).then(function (result) {
          if (result && result.ok) {
            var node = findByFocus(els.shoppingBody, "item-check-" + result.id);
            if (node) {
              node.focus();
            }
          }
        });
      }
    });
    form.appendChild(shopField(Logic.TEXT.addItemName, name));
    form.appendChild(shopField(Logic.TEXT.addItemAmount, amount));
    if (adding.error) {
      form.appendChild(h("p", { class: "notice notice-error", role: "alert", text: adding.error.text }));
    }
    var buttons = h("div", { class: "shop-actions" });
    buttons.appendChild(
      h("button", {
        class: "btn " + (saving ? "btn-quiet" : "btn-primary"),
        type: "submit",
        text: saving ? Logic.TEXT.shoppingSaving : Logic.TEXT.addItemSubmit,
        disabled: locked || completedList,
        "aria-disabled": locked || completedList ? "true" : false,
      })
    );
    buttons.appendChild(
      action(Logic.TEXT.addItemCancel, "btn-quiet", function () {
        if (!saving) {
          controller.cancelShoppingItemAdd();
          els.shoppingTitle.focus();
        }
      })
    );
    form.appendChild(buttons);
    return form;
  }

  // The profile -----------------------------------------------------------------------------------

  function profileLoadingLine(text) {
    return h("p", { class: "loading" }, [
      h("span", { class: "spinner", "aria-hidden": "true" }),
      document.createTextNode(text),
    ]);
  }

  function profileErrorNotice(error, fallback) {
    var box = h("div");
    box.appendChild(h("p", { class: "notice notice-error", role: "alert", text: error ? error.text : fallback }));
    if (error && error.correlationId) {
      box.appendChild(h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", error.correlationId) }));
    }
    return box;
  }

  function profileRetry(onRetry) {
    var button = action(Logic.TEXT.retry, "btn-primary", onRetry, false);
    button.setAttribute("data-focus", "profile-retry");
    return button;
  }

  function profileNameCount(value) {
    return Logic.countCodePoints(Logic.trimLikeServer(value)) + " / " + Logic.MAX_PROFILE_NAME;
  }

  function profileLoginName(method) {
    if (method.provider === "telegram") {
      return Logic.TEXT.profileLoginTelegram;
    }
    if (method.provider === "email") {
      return Logic.TEXT.profileLoginEmail;
    }
    return Logic.TEXT.profileLoginOther;
  }

  function drawProfile(profile) {
    els.profileTitle.textContent = Logic.TEXT.profileTab;
    var box = els.profileBody;
    var focused =
      within(document.activeElement, box) && document.activeElement
        ? document.activeElement.getAttribute("data-focus")
        : null;
    var scroll = box.scrollTop;
    clear(box);
    var content = h("div", { class: "profile-content" });
    box.appendChild(content);

    if (!profile.loaded && (profile.status === "idle" || profile.status === "loading")) {
      content.appendChild(profileLoadingLine(Logic.TEXT.profileLoading));
      box.scrollTop = scroll;
      return;
    }
    if (!profile.loaded && profile.status === "error") {
      content.appendChild(profileErrorNotice(profile.error, Logic.TEXT.profileFailed));
      content.appendChild(h("p", { class: "hint", text: Logic.TEXT.profileOthersWork }));
      content.appendChild(profileRetry(profileReload));
      box.scrollTop = scroll;
      return;
    }

    if (profile.status === "error") {
      content.appendChild(profileErrorNotice(profile.error, Logic.TEXT.profileFailed));
      content.appendChild(profileRetry(profileReload));
    }
    if (profile.notice) {
      var noticeClass = profile.notice.kind === "error" ? "notice notice-error" : "notice notice-ok";
      content.appendChild(
        h("p", {
          class: noticeClass,
          role: profile.notice.kind === "error" ? "alert" : "status",
          text: profile.notice.text,
        })
      );
      if (profile.notice.kind === "error" && profile.notice.correlationId) {
        content.appendChild(
          h("p", { class: "hint", text: Logic.TEXT.correlation.replace("{id}", profile.notice.correlationId) })
        );
      }
      if (profile.notice.kind === "error" && lastView && lastView.section === "profile") {
        alertText(profile.notice.text);
      }
    }
    if (profile.editing) {
      content.appendChild(profileForm(profile));
    } else {
      content.appendChild(profileSummary(profile));
      content.appendChild(profileMeasuresSummary(profile));
    }
    content.appendChild(profileLoginMethods(profile));
    content.appendChild(profileData());
    box.scrollTop = scroll;
    if (focused) {
      var again = findByFocus(box, focused);
      if (again) {
        again.focus();
      } else {
        // The focused control was the one whose action changed the pane's mode (or the form
        // that a successful save replaced): keep the keyboard in the pane, on the control
        // that now does that work.
        var next = findByFocus(box, profile.editing ? "profile-name" : "profile-edit");
        if (next) {
          next.focus();
        }
      }
    }
  }

  function profileReload() {
    controller.reloadProfile();
  }

  function profileBeginEdit() {
    wantFocus = "profile-name"; // the form is drawn by this change, so its field is focused then
    controller.beginProfileEdit();
  }

  var PROFILE_MEASURES = [
    ["glass_ml", Logic.TEXT.profileMeasureGlass],
    ["teaspoon_ml", Logic.TEXT.profileMeasureTeaspoon],
    ["tablespoon_ml", Logic.TEXT.profileMeasureTablespoon],
  ];

  // One measure as the pane shows it: the effective volume and whether it is the system
  // default or the person's own size. The two states are told apart by their words and by
  // a badge of their own, not by colour alone.
  function profileMeasureSetting(setting) {
    var personal = setting.override !== null;
    return h("dd", null, [
      h("span", { class: "profile-measure-value", text: setting.effective + " мл" }),
      h("span", {
        class: "profile-measure-state " + (personal ? "profile-measure-state-personal" : "profile-measure-state-default"),
        text: personal ? Logic.TEXT.profileMeasurePersonal : Logic.TEXT.profileMeasureDefault,
      }),
    ]);
  }

  function profileMeasuresSummary(profile) {
    var values = h("dl", { class: "profile-values profile-measure-values" });
    PROFILE_MEASURES.forEach(function (entry) {
      values.appendChild(
        h("div", { class: "profile-value" }, [
          h("dt", { text: entry[1] }),
          profileMeasureSetting(profile.measures[entry[0]]),
        ])
      );
    });
    return h("section", { class: "profile-section profile-measures-summary" }, [
      h("h2", { text: Logic.TEXT.profileMeasuresTitle }),
      values,
    ]);
  }

  // The saved settings, as the pane opens. Only values the server returned are drawn here:
  // a draft exists only inside the form, so nothing here can look saved before it is.
  function profileSummary(profile) {
    var values = h("dl", { class: "profile-values" });
    function row(label, value, missing) {
      values.appendChild(
        h("div", { class: "profile-value" }, [
          h("dt", { text: label }),
          h("dd", missing ? { class: "profile-not-set", text: value } : { text: value }),
        ])
      );
    }
    row(Logic.TEXT.profileNameLabel, profile.displayName || Logic.TEXT.profileNameNotSet, !profile.displayName);
    row(
      Logic.TEXT.profileTextSizeLabel,
      profile.textSize === "large" ? Logic.TEXT.profileSizeLarge : Logic.TEXT.profileSizeNormal,
      false
    );
    var section = h("div", { class: "profile-summary" }, [values]);
    var edit = action(Logic.TEXT.profileEdit, "btn-primary", profileBeginEdit);
    edit.setAttribute("data-focus", "profile-edit");
    section.appendChild(edit);
    return section;
  }

  // The form's string for one measure: empty when no override is stored, otherwise the
  // stored whole-millilitre value. A draft (typed but not saved) wins, so a redraw keeps it.
  function profileMeasureDraft(profile, name) {
    if (profile.draft) {
      return profile.draft.measures[name];
    }
    var setting = profile.measures[name];
    return setting.override === null ? "" : String(setting.override);
  }

  // The three measure fields, inside the same form and the same Save as everything else.
  // Each field says its approved range and that an empty field means the default, and has
  // its own way back to the default (which only clears the field; Save is still one action).
  function profileMeasureFields(profile, saving, collect) {
    var inputs = [];
    var fieldset = h("fieldset", { class: "profile-fieldset profile-measures" }, [
      h("legend", { text: Logic.TEXT.profileMeasuresTitle }),
    ]);
    PROFILE_MEASURES.forEach(function (entry) {
      var name = entry[0];
      var label = entry[1];
      var limit = Logic.MEASURE_LIMITS[name];
      var id = "profile-measure-" + name;
      var input = h("input", {
        id: id,
        type: "text",
        inputmode: "numeric",
        pattern: "[0-9]*",
        value: profileMeasureDraft(profile, name),
        readonly: saving,
        autocomplete: "off",
        autocapitalize: "none",
        autocorrect: "off",
        spellcheck: "false",
        "data-focus": "profile-measure-" + name,
        "aria-describedby": id + "-hint" + (profile.measureError ? " profile-measures-error" : ""),
      });
      if (profile.measureError) {
        input.setAttribute("aria-invalid", "true");
      }
      input.addEventListener("input", function () {
        controller.syncProfileDraft(collect());
        controller.clearProfileNotice();
      });
      var hint = h("span", {
        id: id + "-hint",
        class: "hint",
        text: Logic.TEXT.profileMeasureHint
          .replace("{min}", String(limit.min))
          .replace("{max}", String(limit.max))
          .replace("{def}", String(limit.def)),
      });
      var reset = action(
        Logic.TEXT.profileMeasureReset,
        "btn-quiet profile-measure-reset",
        function () {
          input.value = "";
          controller.syncProfileDraft(collect());
          controller.clearProfileNotice();
        },
        Logic.TEXT.profileMeasureResetLabel.replace("{name}", label)
      );
      reset.disabled = saving;
      reset.setAttribute("data-focus", "profile-measure-reset-" + name);
      inputs.push({ name: name, input: input });
      fieldset.appendChild(
        h("div", { class: "profile-measure-field" }, [
          h("label", { class: "profile-field", for: id }, [
            h("span", { class: "edit-label", text: label }),
            input,
            hint,
          ]),
          reset,
        ])
      );
    });
    if (profile.measureError) {
      fieldset.appendChild(
        h("span", {
          id: "profile-measures-error",
          class: "field-error",
          role: "alert",
          text: profile.measureError,
        })
      );
    }
    return { node: fieldset, inputs: inputs };
  }

  function profileForm(profile) {
    var saving = profile.saving;
    var shownName = profile.draft ? profile.draft.displayName : profile.displayName || "";
    var shownSize = profile.draft ? profile.draft.textSize : profile.textSize;
    var form = h("form", { class: "profile-form", novalidate: true, "aria-label": Logic.TEXT.profileTab });
    var nameInput = h("input", {
      id: "profile-name",
      type: "text",
      value: shownName,
      readonly: saving,
      autocomplete: "off",
      autocapitalize: "words",
      autocorrect: "off",
      spellcheck: "false",
      "data-focus": "profile-name",
      "aria-describedby": profile.fieldError ? "profile-name-hint profile-name-error" : "profile-name-hint",
    });
    if (profile.fieldError) {
      nameInput.setAttribute("aria-invalid", "true");
    }
    // The count is a convenience beside the field, not a live region: announcing every
    // keystroke would talk over the person writing their name (the composer's counter is
    // hidden from a screen reader for the same reason).
    var count = h("span", { id: "profile-name-count", class: "hint", "aria-hidden": "true", text: profileNameCount(shownName) });
    nameInput.addEventListener("input", function () {
      count.textContent = profileNameCount(nameInput.value);
      controller.syncProfileDraft(collect());
      controller.clearProfileNotice();
    });
    var nameField = h("label", { class: "profile-field", for: "profile-name" }, [
      h("span", { class: "edit-label", text: Logic.TEXT.profileNameLabel }),
      nameInput,
      h("span", { id: "profile-name-hint", class: "hint" }, [
        document.createTextNode(Logic.TEXT.profileNameHint + " "),
        count,
      ]),
    ]);
    if (profile.fieldError) {
      nameField.appendChild(h("span", { id: "profile-name-error", class: "field-error", role: "alert", text: profile.fieldError }));
    }
    form.appendChild(nameField);

    function choice(value, labelText, checked) {
      var input = h("input", {
        type: "radio",
        name: "text-size",
        value: value,
        disabled: saving,
        "data-focus": "profile-size-" + value,
      });
      input.checked = checked;
      input.addEventListener("change", function () {
        controller.syncProfileDraft(collect());
        controller.clearProfileNotice();
      });
      // The whole label is the control's target; the input is what says which one is chosen.
      return { input: input, label: h("label", { class: "profile-choice" }, [input, h("span", { text: labelText })]) };
    }

    var normal = choice("normal", Logic.TEXT.profileSizeNormal, shownSize !== "large");
    var large = choice("large", Logic.TEXT.profileSizeLarge, shownSize === "large");
    form.appendChild(
      h("fieldset", { class: "profile-fieldset" }, [
        h("legend", { text: Logic.TEXT.profileTextSizeLabel }),
        normal.label,
        large.label,
      ])
    );

    var measures = profileMeasureFields(profile, saving, collect);
    form.appendChild(measures.node);

    function collect() {
      var measureValues = {};
      measures.inputs.forEach(function (entry) {
        measureValues[entry.name] = entry.input.value;
      });
      return {
        displayName: nameInput.value,
        textSize: large.input.checked ? "large" : "normal",
        measures: measureValues,
      };
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (saving) {
        return;
      }
      var draft = collect();
      // Which field the refusal will be about, so the redrawn form can put the keyboard
      // there: the name first, then the first measure the page itself would refuse.
      var nameCheck = Logic.prepareProfileName(draft.displayName);
      var measureCheck = Logic.prepareMeasures(draft.measures);
      if (!nameCheck.ok) {
        wantFocus = "profile-name";
      } else if (!measureCheck.ok) {
        wantFocus = "profile-measure-" + measureCheck.field;
      }
      controller.saveProfile(draft).then(function (result) {
        if (result && !result.ok && result.reason === "invalid" && nameCheck.ok && measureCheck.ok) {
          nameInput.focus();
        }
      });
    });

    var actions = h("div", { class: "profile-actions" });
    var save = h("button", {
      class: "btn " + (saving ? "btn-quiet" : "btn-primary"),
      type: "submit",
      text: saving ? Logic.TEXT.profileSaving : Logic.TEXT.profileSave,
      disabled: saving,
      "aria-disabled": saving ? "true" : false,
      "data-focus": "profile-save",
    });
    actions.appendChild(save);
    form.appendChild(actions);
    return form;
  }

  function profileLoginMethods(profile) {
    var section = h("section", { class: "profile-section" }, [
      h("h2", { text: Logic.TEXT.profileLoginTitle }),
      h("p", { class: "hint", text: Logic.TEXT.profileLoginHint }),
    ]);
    if (!profile.loginMethods.length) {
      section.appendChild(h("p", { class: "hint", text: Logic.TEXT.profileLoginEmpty }));
      return section;
    }
    var methods = h("ul", { class: "profile-methods" });
    profile.loginMethods.forEach(function (method) {
      var revoked = method.status === "revoked";
      var row = h("li", { class: "profile-method" }, [
        h("span", { class: "profile-method-name", text: profileLoginName(method) }),
        method.provider === "email" && method.address
          ? h("span", { class: "profile-method-address", text: method.address })
          : null,
        h("span", {
          class: "profile-badge " + (revoked ? "profile-badge-revoked" : "profile-badge-active"),
          text: revoked ? Logic.TEXT.profileStatusRevoked : Logic.TEXT.profileStatusActive,
        }),
      ]);
      methods.appendChild(row);
    });
    section.appendChild(methods);
    return section;
  }

  function profileData() {
    var section = h("section", { class: "profile-section profile-data" }, [
      h("h2", { text: Logic.TEXT.profileDataTitle }),
    ]);
    [
      Logic.TEXT.profileDataMessages,
      Logic.TEXT.profileDataRecipes,
      Logic.TEXT.profileDataShopping,
      Logic.TEXT.profileDataOpenAI,
      Logic.TEXT.profileDataDelete,
      Logic.TEXT.profileDataProfile,
    ].forEach(function (text) {
      section.appendChild(h("p", { text: text }));
    });
    return section;
  }

  els.profileOpen.addEventListener("click", function () {
    setDrawer(false, false);
    wantFocus = "profile";
    controller.showSection("profile");
  });

  // Opening a saved list (or beginning another draft) gives up a modified shopping draft:
  // ask first, unless the browser has nobody to ask (the call is best effort), as for an
  // open recipe editor. The question is only asked by an explicit navigation, never by a
  // redraw, and a refused question leaves the draft and its marks exactly as they were.
  function leaveShoppingDraft() {
    if (!controller.shoppingDraftDirty()) {
      return true;
    }
    if (typeof window.confirm !== "function") {
      return true;
    }
    return window.confirm(Logic.TEXT.shoppingDraftUnsaved);
  }

  function openShoppingList(id) {
    if (!leaveShoppingDraft()) {
      return;
    }
    setDrawer(false, false); // the navigation was accepted: the drawer gives way to the list
    controller.openShoppingList(id).then(function (result) {
      if (result && result.ok) {
        els.shoppingTitle.focus();
      }
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
        var recipeActionsRow = h("div", { class: "recipe-actions" }, [
          action(Logic.TEXT.editAction, "btn-secondary", function () {
            wantFocus = "edit-title";
            controller.beginEditFavorite();
          }),
        ]);
        if (current.kind === "structured") {
          recipeActionsRow.appendChild(
            action(Logic.TEXT.shoppingDraftTitle, "btn-secondary", function () {
              if (!leaveShoppingDraft()) {
                return;
              }
              var result = controller.beginShoppingDraftFromFavorite();
              if (result && result.ok) {
                setDrawer(false, false);
                els.shoppingTitle.focus();
              }
            }, "Составить список покупок: " + current.title)
          );
        }
        box.appendChild(recipeActionsRow);
        box.appendChild(removalControls(current));
      }
    }
    box.scrollTop = scrolled;
  }

  // One ingredient's conversion as the line under it, or null when there is nothing to
  // show. "Точно" is the exact rational the core computed for the established source and
  // target capacities (not a claim about a real spoon). The approximation marker appears
  // only when the practical rounded value actually differs from the exact one.
  function measureHint(conversion) {
    if (!conversion || conversion.identical || conversion.presentation === "none") {
      return null;
    }
    // A number is only ever drawn for a conversion the core actually made. The logic
    // already refuses a block whose status and presentation contradict each other; this
    // is the last guard before a number becomes words on the page.
    if (conversion.status !== "exact" && conversion.status !== "nominal_household") {
      return null;
    }
    if (conversion.presentation === "exact") {
      if (!conversion.exact) {
        return null;
      }
      var exact = Logic.formatMeasure(
        conversion.exact.numerator,
        conversion.exact.denominator,
        conversion.targetUnit
      );
      return exact === null ? null : Logic.TEXT.measureExact.replace("{value}", exact);
    }
    if (conversion.presentation === "rounded") {
      if (!conversion.rounded) {
        return null;
      }
      var rounded = Logic.formatMeasure(
        conversion.rounded.numerator,
        conversion.rounded.denominator,
        conversion.targetUnit
      );
      if (rounded === null) {
        return null;
      }
      var changed = conversion.rounded.relativeError.numerator !== 0;
      return changed ? Logic.TEXT.measureApprox.replace("{value}", rounded) : rounded;
    }
    return null;
  }

  // KH-F01-B2: one ingredient's mass estimate as the line under it, or null when the
  // server's section carries no number the page may show. Every mass-derived number is
  // approximate -- a density or an individual piece weight is a reference, not a
  // measurement of this amount -- so the hint always says "≈". A conditional estimate
  // carries its validated Russian assumption beside the number, never only in metadata.
  function massHint(conversion) {
    if (!conversion) {
      return null;
    }
    var value = null;
    if (conversion.kind === "volume") {
      value = Logic.formatMassMeasure(
        conversion.numerator,
        conversion.denominator,
        conversion.targetUnit
      );
    } else if (conversion.kind === "count") {
      var low = Logic.formatFraction(conversion.low.numerator, conversion.low.denominator);
      if (low === null) {
        return null;
      }
      if (conversion.display === "single") {
        value = low + " " + Logic.TEXT.massCountUnit;
      } else {
        var high = Logic.formatFraction(conversion.high.numerator, conversion.high.denominator);
        if (high === null) {
          return null;
        }
        value = low + "–" + high + " " + Logic.TEXT.massCountUnit;
      }
    }
    if (value === null) {
      return null;
    }
    var text = filledText(Logic.TEXT.measureApprox, { value: value });
    if (conversion.assumptionLabel !== null && conversion.assumptionLabel !== undefined) {
      text += ", " + conversion.assumptionLabel;
    }
    return text;
  }

  // One step amount's hint, or null when the server's data does not carry a number this
  // page may show. A hint is drawn only for an amount that was really linked and really
  // converted; an unresolved, ambiguous, shared, unsupported, mass or failed amount keeps
  // its original text and gets no number. A recognized source word ("около") makes the
  // hint approximate even when the rounded value is numerically exact (15.7.3).
  function stepMeasureHint(span) {
    var conversion = span.conversion;
    if (span.link !== "linked" || !conversion) {
      return null;
    }
    // A number exists only for a capacity the server established (declared in the text,
    // supplied for this request, or not needed at all). An unresolved capacity is always
    // drawn as the original text, never as a number.
    if (span.capacityOrigin === "unresolved") {
      return null;
    }
    // `identical` and `presentation: "none"` mean there is nothing new to say.
    if (conversion.identical || conversion.presentation === "none") {
      return null;
    }
    if (conversion.status !== "exact" && conversion.status !== "nominal_household") {
      return null;
    }
    var fraction = conversion.presentation === "exact" ? conversion.exact : conversion.rounded;
    if (!fraction) {
      return null;
    }
    // The released B1 formatting: the fraction and the Russian grammatical form of the unit.
    var value = Logic.formatMeasure(fraction.numerator, fraction.denominator, conversion.targetUnit);
    if (value === null) {
      return null;
    }
    if (span.sourceApproximate) {
      return Logic.TEXT.measureApprox.replace("{value}", value);
    }
    if (conversion.presentation === "exact") {
      return Logic.TEXT.measureExact.replace("{value}", value);
    }
    return conversion.rounded.relativeError.numerator !== 0
      ? Logic.TEXT.measureApprox.replace("{value}", value)
      : value;
  }

  // One step with its hints: the stored text is a run of text nodes exactly as it was
  // stored, and each hint is a separate element placed after the amount span it belongs to.
  // Nothing is rewritten, normalized or inserted into the string, and no recipe text is
  // ever parsed as markup (15.8.4). A span whose slice does not match the text the page
  // holds (which the logic already refuses) is skipped rather than trusted.
  function stepLine(step, annotation, dropClarified) {
    var children = [];
    var at = 0;
    annotation.spans.forEach(function (span) {
      // After the safe «Не знаю» answer, a hint that rests on the withdrawn temporary
      // capacity is not drawn; one from the recipe's own declaration still is.
      if (dropClarified && span.capacityOrigin === "temporary_clarification") {
        return;
      }
      var start16 = Logic.codePointToUtf16(step, span.start);
      var end16 = Logic.codePointToUtf16(step, span.end);
      if (start16 < 0 || end16 <= start16 || end16 < at) {
        return;
      }
      if (step.slice(start16, end16) !== span.text) {
        return;
      }
      children.push(document.createTextNode(step.slice(at, start16)));
      children.push(document.createTextNode(step.slice(start16, end16)));
      at = end16;
      var hint = stepMeasureHint(span);
      if (hint !== null) {
        children.push(h("span", { class: "measure-hint recipe-step-hint", text: " (" + hint + ")" }));
      }
    });
    children.push(document.createTextNode(step.slice(at)));
    return h("li", null, children);
  }

  // "Which glass does the recipe mean?" -- asked only when an amount contains an
  // undeclared source glass. Nothing is preselected: the person's own glass is never
  // assumed to be the recipe's. The answer applies to this recipe's presentation only;
  // it is not saved to the profile or the recipe.
  //
  // The confirmed capacity and the custom field are separate states. A radio is checked
  // only when the server confirmed that capacity together with the conversion drawn under
  // it; the custom field is opened by a disclosure button and holds a draft that confirms
  // nothing until "Применить" has the server's answer. Opening the field or typing into
  // it therefore never contradicts the conversion on the card.
  function sourceGlassChooser(current) {
    var measures = current.measures;
    // The confirmed choice: its block is the one drawn, so this control never shows a
    // capacity whose conversion is not the one on the card. A choice under way is named
    // as pending; the confirmed one stays selected until its answer arrives.
    var chosen = measures.sourceGlassMl;
    var custom = chosen !== null && chosen !== 200 && chosen !== 250;
    var fieldset = h("fieldset", { class: "measure-clarify" }, [
      h("legend", { text: Logic.TEXT.sourceGlassQuestion }),
      // What is missing and why a hint is absent -- said once per recipe, not per amount.
      h("p", { class: "hint", text: Logic.TEXT.sourceGlassExplanation }),
    ]);
    function option(value, label, id) {
      var input = h("input", {
        type: "radio",
        name: "source-glass-option",
        value: String(value),
        id: id,
        "data-focus": id,
      });
      input.checked = chosen === value;
      input.addEventListener("change", function () {
        if (input.checked) {
          wantFocus = id; // the redraw keeps the keyboard on the choice
          controller.setSourceGlass(value);
        }
      });
      return h("label", { class: "profile-choice", for: id }, [input, h("span", { text: label })]);
    }
    fieldset.appendChild(option(200, Logic.TEXT.sourceGlass200, "source-glass-200"));
    fieldset.appendChild(option(250, Logic.TEXT.sourceGlass250, "source-glass-250"));

    var otherId = "source-glass-other";
    var panelId = "source-glass-other-panel";
    var hintId = "source-glass-draft-hint";
    var open = current.sourceGlassOtherOpen === true;
    var pending = current.sourceGlassPending !== null;

    var toggle = action(Logic.TEXT.sourceGlassOther, "btn-quiet", function () {
      var now = controller.toggleSourceGlassOther();
      // The redraw replaces the disclosure, so the keyboard follows it: into the field it
      // just opened, or back to the button that closed it.
      wantFocus = now && now.open ? otherId : "source-glass-other-toggle";
    });
    toggle.setAttribute("id", "source-glass-other-toggle");
    toggle.setAttribute("data-focus", "source-glass-other-toggle");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-controls", panelId);

    var otherInput = h("input", {
      id: otherId,
      type: "text",
      inputmode: "numeric",
      pattern: "[0-9]*",
      autocomplete: "off",
      autocapitalize: "none",
      autocorrect: "off",
      spellcheck: "false",
      value: current.sourceGlassDraft,
      "aria-describedby": "source-glass-hint " + hintId + " source-glass-error",
      "data-focus": otherId,
    });
    var draftHint = h("p", { id: hintId, class: "hint", text: Logic.TEXT.sourceGlassDraftHint });
    // The hint says what the field is: a draft, not a choice. It is not shown for an empty
    // field or for the already confirmed capacity (there is nothing left to apply).
    function syncDraftHint() {
      var value = otherInput.value;
      draftHint.hidden = value.trim() === "" || value === (chosen === null ? "" : String(chosen));
    }
    otherInput.addEventListener("input", function () {
      // Kept for the next redraw, not confirmed by existing: typing changes no selection.
      controller.syncSourceGlassDraft(otherInput.value);
      syncDraftHint();
    });
    syncDraftHint();

    var error = h("span", {
      id: "source-glass-error",
      class: "field-error",
      role: "alert",
      hidden: true,
    });
    var apply = action(Logic.TEXT.sourceGlassApply, "btn-secondary", function () {
      error.hidden = true;
      otherInput.removeAttribute("aria-invalid");
      wantFocus = "source-glass-apply"; // the redraw keeps the keyboard here
      controller.syncSourceGlassDraft(otherInput.value);
      var result = controller.setSourceGlass(otherInput.value);
      if (result && typeof result.then === "function") {
        result.then(function (outcome) {
          // Nothing was drawn for a refusal that sent nothing: no redraw is coming to
          // consume the focus this handler asked for.
          if (
            outcome &&
            (outcome.reason === "invalid" || outcome.reason === "editing" || outcome.reason === "not_ready")
          ) {
            wantFocus = null;
          }
          // Only an unusable volume is the field's own error; a failed read is said over
          // the card, with the confirmed choice and its conversion still in place.
          if (outcome && outcome.reason === "invalid") {
            error.textContent = Logic.TEXT.sourceGlassInvalid;
            error.hidden = false;
            otherInput.setAttribute("aria-invalid", "true");
            otherInput.focus();
          }
        });
      }
    });
    apply.setAttribute("data-focus", "source-glass-apply");

    var panel = h("div", {
      class: "measure-other",
      id: panelId,
      hidden: !open,
      "aria-busy": pending ? "true" : "false",
    });
    panel.appendChild(
      h("label", { class: "visually-hidden", for: otherId, text: Logic.TEXT.sourceGlassOtherLabel })
    );
    panel.appendChild(otherInput);
    panel.appendChild(apply);
    panel.appendChild(draftHint);

    fieldset.appendChild(
      h("div", { class: "measure-other" }, [
        toggle,
        // A custom capacity the server confirmed is named as what it is: the field beside
        // it holds that very value when it is reopened.
        custom
          ? h("span", {
              class: "measure-hint",
              text: filledText(Logic.TEXT.sourceGlassConfirmed, { value: chosen }),
            })
          : null,
      ])
    );
    fieldset.appendChild(panel);
    // The safe answer (15.7.4): no capacity is guessed, the question closes, and every hint
    // that rested on a temporary capacity of this view is dropped -- the declared ones stay.
    var unknown = action(Logic.TEXT.sourceGlassUnknown, "btn-quiet", function () {
      // The control disappears with the question: the keyboard goes to the card's title.
      wantFocus = "recipe";
      controller.chooseUnknownSourceGlass();
    });
    unknown.setAttribute("id", "source-glass-unknown");
    unknown.setAttribute("data-focus", "source-glass-unknown");
    fieldset.appendChild(h("div", { class: "measure-unknown" }, [unknown]));
    fieldset.appendChild(h("p", { id: "source-glass-hint", class: "hint", text: Logic.TEXT.sourceGlassNote }));
    fieldset.appendChild(error);
    return fieldset;
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
      var conversions = {};
      // Component-level failure states, each true only for its own domain (15.8.2): a
      // provenance failure is not a step failure, and the whole-block summary failing does
      // not by itself say which component did. A step-only claim is made only when the
      // provenance component is present and did not fail; otherwise the summary is said
      // neutrally rather than guessed.
      var provenanceStatus = Boolean(current.measures) ? current.measures.provenanceStatus : null;
      var provenanceFailed = provenanceStatus === "failed";
      var analysisFailed = Boolean(current.measures) && current.measures.analysisStatus === "failed";
      var stepAnalysisFailed =
        analysisFailed && (provenanceStatus === "complete" || provenanceStatus === "incomplete");
      // A safe refresh after «Не знаю» is a pending read like any other, but it must not
      // take independently valid information off the card: while it is out, the block on
      // the card stays drawn and only the lines that may rest on the withdrawn capacity
      // are filtered. Any other pending read (a newer capacity, a save) draws no hint
      // until its own answer arrives.
      if (current.measures && (!current.measuresPending || current.sourceGlassUnknown)) {
        current.measures.ingredients.forEach(function (conversion) {
          // After «Не знаю», a line converted only with the withdrawn temporary capacity is
          // not shown as converted; a declared capacity's conversion still is.
          if (current.sourceGlassUnknown && current.measures.clarifiedPositions[conversion.position] === true) {
            return;
          }
          conversions[conversion.position] = conversion;
        });
      }
      // KH-F01-B2: the mass estimates travel with the same block but rest on no source
      // glass, so the safe «Не знаю» answer withdraws none of them. While a newer read is
      // out they are withheld with every other derived hint, exactly as B1.1 is.
      var massConversions = {};
      if (current.measures && (!current.measuresPending || current.sourceGlassUnknown)) {
        (current.measures.massConversions || []).forEach(function (conversion) {
          massConversions[conversion.position] = conversion;
        });
      }
      var anyHint = false;
      var ingredientList = h(
        "ul",
        { class: "recipe-list" },
        current.ingredients.map(function (ingredient, position) {
          var line = h("span", { class: "recipe-ingredient", text: ingredient.name + " — " + ingredient.amount });
          var hint = measureHint(conversions[position]);
          // One amount carries one hint: a mass estimate is drawn only where the released
          // B1.1 conversion did not already speak for that line.
          var mass = hint === null ? massHint(massConversions[position]) : null;
          if (hint === null && mass === null) {
            return h("li", null, [line]);
          }
          anyHint = true;
          var children = [line];
          if (hint !== null) {
            children.push(h("span", { class: "measure-hint", text: hint }));
          }
          if (mass !== null) {
            children.push(h("span", { class: "measure-hint recipe-mass-hint", text: mass }));
          }
          return h("li", { class: "recipe-ingredient-measured" }, children);
        })
      );
      var ingredientsBody = anyHint
        ? h("div", null, [h("p", { class: "measure-caption", text: Logic.TEXT.measuresTitle }), ingredientList])
        : ingredientList;
      if (provenanceFailed) {
        // The audit of where the ingredient amounts came from failed. The conversions the
        // server did compute are a different component and stay as they are; nothing here
        // says the step amounts were not calculated.
        ingredientsBody = h("div", null, [
          ingredientsBody,
          h("p", { class: "hint", role: "status", text: Logic.TEXT.provenanceNotChecked }),
        ]);
      }
      wrapper.appendChild(recipeSection(Logic.TEXT.ingredients, ingredientsBody));
      // The question is asked once per recipe, when any amount -- an ingredient's or a
      // step's -- contains an undeclared glass. It is not asked again once the safe
      // «Не знаю» answer closed it for this view.
      if (
        current.measures &&
        !current.sourceGlassUnknown &&
        (current.measures.sourceGlassRequired || current.measures.stepsGlassRequired)
      ) {
        wrapper.appendChild(sourceGlassChooser(current));
      }
      // A choice under way is named; the confirmed one and its conversion stay on the card
      // until the server answers for the new one. A failed choice leaves the confirmed one
      // in place and offers to ask again.
      if (current.sourceGlassPending !== null) {
        wrapper.appendChild(
          h("p", {
            class: "hint",
            role: "status",
            text: Logic.TEXT.sourceGlassPending.replace("{value}", String(current.sourceGlassPending)),
          })
        );
      }
      if (current.sourceGlassError) {
        wrapper.appendChild(
          h("p", { class: "notice notice-error", role: "alert", text: current.sourceGlassError.text })
        );
        var retryGlass = action(Logic.TEXT.retry, "btn-secondary", function () {
          var failed = current.sourceGlassError.value;
          if (failed === null) {
            // The re-read after «Не знаю» failed: ask again for the recipe as its own text
            // declares it, with no capacity assumed.
            wantFocus = "recipe";
            controller.retryFavoriteConversion();
            return;
          }
          // The redraw that starts the retry keeps the keyboard on the choice it repeats.
          wantFocus =
            failed === 200
              ? "source-glass-200"
              : failed === 250
                ? "source-glass-250"
                : "source-glass-other";
          controller.setSourceGlass(failed);
        });
        wrapper.appendChild(retryGlass);
      }
      // The card was saved but its conversion had to be read again (with the confirmed
      // choice): until that answer arrives, or after it failed, no old hint is drawn.
      if (current.measuresPending) {
        wrapper.appendChild(h("p", { class: "hint", role: "status", text: Logic.TEXT.measuresRefreshPending }));
      }
      if (current.measuresError) {
        wrapper.appendChild(
          h("p", { class: "notice notice-error", role: "alert", text: current.measuresError.text })
        );
        var retryMeasure = action(Logic.TEXT.retry, "btn-secondary", function () {
          wantFocus = "recipe"; // the redraw replaces this button; the card's title takes the keyboard
          controller.retryFavoriteConversion();
        });
        wrapper.appendChild(retryMeasure);
      }
      // The step hints belong to the block on the card: a read under way, or one whose
      // answer belongs to another version, draws no derived hint at all.
      var stepAnnotations = {};
      if (current.measures && (!current.measuresPending || current.sourceGlassUnknown)) {
        current.measures.steps.forEach(function (annotation) {
          stepAnnotations[annotation.stepIndex] = annotation;
        });
      }
      var stepsBody = h(
        "ol",
        { class: "recipe-list recipe-steps" },
        current.steps.map(function (step, index) {
          var annotation = stepAnnotations[index];
          // The echoed text must be the very step the page is drawing. The logic already
          // checked it; a mismatch here simply means the stored text alone.
          if (!annotation || annotation.text !== step) {
            return h("li", { text: step });
          }
          return stepLine(step, annotation, current.sourceGlassUnknown === true);
        })
      );
      var stepsSectionBody = h("div", null, [stepsBody]);
      if (analysisFailed && !stepAnalysisFailed) {
        // The whole-block summary failed and the step component is not the one the client
        // can name (the provenance audit failed, or its own status is absent). Whether the
        // step pass also failed is not in the payload (empty `steps` is also a pass with no
        // amounts), so the card says the neutral truth instead of claiming step amounts
        // were unavailable.
        stepsSectionBody.appendChild(
          h("p", { class: "hint", role: "status", text: Logic.TEXT.analysisPartlyFailed })
        );
      } else if (stepAnalysisFailed) {
        // The server could not finish its analysis. That is not a statement that every
        // amount was understood: the text is drawn as stored, without speculative numbers.
        stepsSectionBody.appendChild(
          h("p", { class: "hint", role: "status", text: Logic.TEXT.stepsNotChecked })
        );
      }
      wrapper.appendChild(recipeSection(Logic.TEXT.steps, stepsSectionBody));
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
    } else if (recipe.state === "saving") {
      row.appendChild(
        h("p", { class: "msg-note" }, [
          h("span", { class: "spinner", "aria-hidden": "true" }),
          h("span", { text: shown + ": " + Logic.TEXT.recipeSaving }),
        ])
      );
    } else {
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
    }
    // «Что купить?»: every structured recipe can be turned into a shopping list, whatever
    // its Save state; nothing is inferred from conversational text.
    row.appendChild(
      action(Logic.TEXT.shoppingDraftTitle, "btn-quiet", function () {
        if (!leaveShoppingDraft()) {
          return;
        }
        controller
          .beginShoppingDraftFromRecipe(conversationId, messageId, recipe.position)
          .then(function (result) {
            if (result && result.ok) {
              setDrawer(false, false);
              els.shoppingTitle.focus();
            }
          });
      }, "Составить список покупок: " + recipe.title)
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
    // Everything under the drawer is out of reach, not only the conversation: the favorites
    // and the shopping pane are the same kind of background and take no focus or click
    // while the drawer covers them. Closing gives all of them back.
    els.chat.inert = isOpen;
    els.recipe.inert = isOpen;
    els.shopping.inert = isOpen;
    els.profile.inert = isOpen;
    if (isOpen) {
      var activeTab =
        els.tabShopping.getAttribute("aria-pressed") === "true"
          ? els.tabShopping
          : els.tabFavorites.getAttribute("aria-pressed") === "true"
            ? els.tabFavorites
            : els.profileOpen.getAttribute("aria-pressed") === "true"
              ? els.profileOpen
              : els.tabChats;
      activeTab.focus();
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
  els.tabShopping.addEventListener("click", function () {
    controller.showSection("shopping");
  });
  els.favoritesRetry.addEventListener("click", function () {
    controller.reloadFavorites();
  });
  els.shoppingRetry.addEventListener("click", function () {
    controller.reloadShoppingLists();
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
  // question is the best that can be done for an editor or a shopping draft that was not
  // saved. Whether the question is actually shown is the browser's decision.
  window.addEventListener("beforeunload", function (event) {
    if (controller.hasUnsavedChanges() || controller.shoppingDraftDirty()) {
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
