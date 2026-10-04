import assert from "node:assert/strict";
import test from "node:test";

import { ProfilePage } from "../../pages/dashboard/modules/profile-page.js";

function fakeElement() {
  const classes = new Set();
  const listeners = {};
  return {
    innerHTML: "",
    textContent: "",
    value: "",
    disabled: false,
    className: "",
    listeners,
    addEventListener(type, handler) { listeners[type] = handler; },
    closest() { return null; },
    classList: {
      add(name) { classes.add(name); },
      toggle(name, enabled) {
        if (enabled) classes.add(name);
        else classes.delete(name);
      },
      contains(name) { return classes.has(name); },
    },
  };
}

function setup(api) {
  const ids = [
    "profile-refresh", "profile-reset", "profile-status", "profile-scope-note", "profile-scope",
    "profile-session", "profile-key", "profiles-body", "profile-pagination-info",
    "profile-prev", "profile-next", "peek-badge", "peek-title",
    "peek-body", "profile-editor-form", "profile-edit-value", "profile-edit-category",
    "profile-edit-confidence", "profile-edit-expires", "profile-editor-save", "profile-editor-delete",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, fakeElement()]));
  globalThis.document = { getElementById: (id) => elements[id] || null };
  globalThis.window = { t: (key, ...args) => `${key}:${args.join("|")}` };
  const state = {};
  const peek = {
    opens: 0,
    closes: 0,
    confirmed: true,
    open() { this.opens += 1; },
    close() { this.closes += 1; },
    async showConfirmDialog(title, message) {
      this.lastDialog = { title, message };
      return this.confirmed;
    },
  };
  return { page: new ProfilePage(state, api, peek), state, peek, elements };
}

function cleanup() {
  delete globalThis.document;
  delete globalThis.window;
}

const response = {
  items: [{
    profile_scope: "platform:user:chat-A",
    profile_key: "favorite_food",
    category: "preference",
    value: "rice & <script>alert(1)</script>",
    confidence: 0.85,
    source_session_id: "chat-A",
    updated_at: 1_700_000_000,
    expires_at: null,
  }],
  total: 2,
  limit: 1,
  offset: 0,
  scopes: ["platform:user:chat-A", "platform:user:chat-B"],
  session_ids: ["chat-A", "chat-B"],
  profile_enabled: true,
  scope_mode: "session",
};

test("profile page lists scoped facts, escapes stored text, and paginates", async () => {
  const gets = [];
  const { page, state, elements } = setup({
    async get(path, params) { gets.push({ path, params }); return response; },
  });
  try {
    state.profile.limit = 1;
    state.profile.scope = "platform:user:chat-A";
    state.profile.sessionId = "chat-A";
    state.profile.key = "favorite";
    await page.fetch();
    assert.deepEqual(gets, [{
      path: "profiles",
      params: { limit: "1", offset: "0", scope: "platform:user:chat-A", session_id: "chat-A", key: "favorite" },
    }]);
    assert.match(elements["profiles-body"].innerHTML, /favorite_food/);
    assert.match(elements["profiles-body"].innerHTML, /rice &amp; &lt;script&gt;/);
    // Layout wrappers must stay inside td; browser fixture verifies computed CSS.
    assert.match(elements["profiles-body"].innerHTML, /<td class="profile-fact-cell"[^>]*><div class="profile-fact-content">/);
    assert.match(elements["profiles-body"].innerHTML, /<td class="profile-value-cell"[^>]*><div class="profile-value-content">/);
    assert.match(elements["profiles-body"].innerHTML, /<td class="profile-chat-cell"[^>]*><div class="profile-chat-content">/);
    assert.match(elements["profiles-body"].innerHTML, /class="btn btn-danger btn-sm profile-delete"[^>]*aria-label="profile.deleteTitle:/);
    assert.doesNotMatch(elements["profiles-body"].innerHTML, /<script>/);
    assert.match(elements["profile-scope-note"].textContent, /profile.scopeMode.session/);
    assert.equal(elements["profile-prev"].disabled, true);
    assert.equal(elements["profile-next"].disabled, false);
  } finally {
    cleanup();
  }
});

test("profile rows expose recent group context and editor saves exact scope/key", async () => {
  const posts = [];
  const { page, state, elements, peek } = setup({
    async get() {
      return {
        ...response,
        items: [{ ...response.items[0], source_group_id: "42", source_group_name: "夜猫子游戏群", source_sender_name: "Alice" }],
        total: 1,
      };
    },
    async post(path, body, options) {
      posts.push({ path, body, options });
      return { profile_key: body.profile_key, value: body.value };
    },
  });
  try {
    await page.fetch();
    assert.match(elements["profiles-body"].innerHTML, /夜猫子游戏群/);
    const item = state.profile.items[0];
    page.openItem(item);
    assert.equal(peek.opens, 1);
    elements["profile-edit-value"].value = "new value";
    elements["profile-edit-category"].value = "preference";
    elements["profile-edit-confidence"].value = "0.91";
    elements["profile-edit-expires"].value = "";
    await page.saveItem(item);
    assert.deepEqual(posts[0], {
      path: "profiles/update",
      body: {
        profile_scope: "platform:user:chat-A",
        profile_key: "favorite_food",
        value: "new value",
        category: "preference",
        confidence: 0.91,
        expires_at: null,
      },
      options: { retries: 0 },
    });
  } finally {
    cleanup();
  }
});

test("deletion sends exact scope and key only after confirmation", async () => {
  const posts = [];
  const { page, peek } = setup({
    async get() { return { ...response, total: 0, items: [] }; },
    async post(path, body, options) {
      posts.push({ path, body, options });
      return { deleted: true, count: 1 };
    },
  });
  try {
    peek.confirmed = false;
    await page.deleteItem("platform:user:chat-A", "favorite_food");
    assert.equal(posts.length, 0);
    assert.equal(peek.closes, 1);

    peek.confirmed = true;
    await page.deleteItem("platform:user:chat-B", "favorite_food");
    assert.deepEqual(posts, [{
      path: "profiles/delete",
      body: { profile_scope: "platform:user:chat-B", profile_key: "favorite_food" },
      options: { retries: 0 },
    }]);
    assert.match(peek.lastDialog.message, /platform:user:chat-B/);
    assert.equal(peek.closes, 2);
  } finally {
    cleanup();
  }
});

test("unknown confidence and scope mode remain explicit", async () => {
  const { page, elements } = setup({
    async get() {
      return {
        ...response,
        items: [{ ...response.items[0], confidence: null }],
        scope_mode: "future_mode",
      };
    },
  });
  try {
    await page.fetch();
    assert.match(elements["profiles-body"].innerHTML, /table.na/);
    assert.match(elements["profile-scope-note"].textContent, /profile.scopeMode.unknown:future_mode/);
  } finally {
    cleanup();
  }
});

test("a selected scope stays visible after its last fact is removed", async () => {
  const { page, state, elements } = setup({
    async get() {
      return { ...response, items: [], total: 0, scopes: [], session_ids: [] };
    },
  });
  try {
    state.profile.scope = "platform:user:chat-A";
    state.profile.sessionId = "chat-A";
    await page.fetch();
    assert.equal(elements["profile-scope"].value, "platform:user:chat-A");
    assert.match(elements["profile-scope"].innerHTML, /platform:user:chat-A/);
    assert.equal(elements["profile-session"].value, "chat-A");
  } finally {
    cleanup();
  }
});
