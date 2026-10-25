import assert from "node:assert/strict";
import test from "node:test";
import { ProfilePage } from "../../pages/dashboard/modules/profile-page.js";
function element() {
  const classes = new Set();
  return { innerHTML: "", textContent: "", value: "", disabled: false, listeners: {}, focus() {},
    replaceChildren(...nodes) { this.children = nodes; },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    classList: { add: name => classes.add(name), remove: name => classes.delete(name), toggle: (name, yes) => yes ? classes.add(name) : classes.delete(name), contains: name => classes.has(name) } };
}
function setup(api) {
  const ids = ["profile-refresh", "profile-status", "profile-scope-note", "profile-users", "profile-reader", "profile-pagination-info", "profile-prev", "profile-next", "peek-badge", "peek-title", "peek-body", "profile-editor-form", "profile-editor-delete", "profile-editor-source", "profile-edit-value", "profile-edit-category", "profile-edit-confidence", "profile-edit-expires", "profile-editor-save", "profile-editor-feedback"];
  const elements = Object.fromEntries(ids.map(id => [id, element()]));
  globalThis.document = { getElementById: id => elements[id] || null };
  globalThis.window = { t: (key, ...args) => `${key}:${args.join("|")}` };
  const peek = { _detailGeneration: 0, open() {}, close() {}, confirmed: true, async showConfirmDialog() { return this.confirmed; } };
  const state = {}; return { page: new ProfilePage(state, api, peek), state, peek, elements };
}
const user = { identity: "opaque:user:alice", platform: "qq", sender_id: "100", sender_name: "Alice <img>", last_group_name: "夜猫子游戏群", fact_count: 3, scope_count: 2 };
const facts = [
  { profile_scope: "scope:A", profile_key: "favorite_food", category: "preference", value: "rice & <script>alert(1)</script>", source_group_name: "游戏群", source_memory_id: 42, updated_at: 1700000000, confidence: .9 },
  { profile_scope: "scope:B", profile_key: "preferred_nickname", category: "identity", value: "Alice", source_group_name: "读书群", expires_at: null },
];
const directory = { users: [user], total: 2, limit: 1, offset: 0, has_more: true, profile_enabled: true, scope_mode: "session" };
const detail = { items: facts, total: 3, has_more: true };
test("directory paginates users; one identity reads separate exact scopes with escaped full text", async () => {
  const calls = [], { page, state, elements } = setup({ async get(path, params) { calls.push({ path, params }); return path === "profiles/users" ? directory : detail; } });
  state.profile.limit = 1; state.profile.query = "Alice"; state.profile.sessionId = "chat-A";
  await page.fetch();
  assert.deepEqual(calls, [{ path: "profiles/users", params: { limit: "1", offset: "0", q: "Alice", session_id: "chat-A" } }, { path: "profiles", params: { identity: user.identity, limit: "500", offset: "0" } }]);
  assert.equal((elements["profile-users"].innerHTML.match(/data-profile-user=/g) || []).length, 1);
  assert.match(elements["profile-users"].innerHTML, /Alice &lt;img&gt;/);
  assert.match(elements["profile-users"].innerHTML, /夜猫子游戏群/);
  assert.equal((elements["profile-reader"].innerHTML.match(/class="profile-source-section"/g) || []).length, 2);
  assert.match(elements["profile-reader"].innerHTML, /scope:A/); assert.match(elements["profile-reader"].innerHTML, /scope:B/);
  assert.match(elements["profile-reader"].innerHTML, /profile.keyLabel.food/);
  assert.match(elements["profile-reader"].innerHTML, /rice &amp; &lt;script&gt;/);
  assert.doesNotMatch(elements["profile-reader"].innerHTML, /<script>/);
  assert.equal(elements["profile-next"].disabled, false);
  assert.match(elements["profile-reader"].innerHTML, /data-source-memory="42"/);
});
test("fact continuation uses identity and loaded offset without splitting the user directory", async () => {
  const calls = [], { page, state } = setup({ async get(path, params) { calls.push(params); return params.offset === "0" ? detail : { items: [{ ...facts[0], profile_key: "hobbies" }], total: 3, has_more: false }; } });
  state.profile.identity = user.identity; state.profile.user = user;
  await page.fetchDetail(); await page.fetchDetail(true);
  assert.equal(calls[1].offset, "2"); assert.equal(calls[1].identity, user.identity);
  assert.equal(state.profile.items.length, 3); assert.equal(state.profile.detailHasMore, false);
});
test("lifetime is explicit and suppressed facts never look active", () => {
  const { page } = setup({});
  assert.equal(page.lifecycle({ expires_at: null }, 100), "stable");
  assert.equal(page.lifecycle({ expires_at: 200 }, 100), "temporary");
  assert.equal(page.lifecycle({ expires_at: 99 }, 100), "expired");
  assert.equal(page.lifecycle({ suppressed: true, expires_at: 200 }, 100), "suppressed");
});
test("saving and confirmed deletion keep the exact original scope/key", async () => {
  const posts = [], { page, peek, elements } = setup({ async get(path) { return path === "profiles/users" ? directory : detail; }, async post(path, body, options) { posts.push({ path, body, options }); return { count: 1 }; } });
  page.openItem(facts[0]);
  assert.equal(peek._detailGeneration, 1);
  elements["profile-edit-value"].value = "new value"; elements["profile-edit-category"].value = "preference";
  elements["profile-edit-confidence"].value = "0.85";
  await page.saveItem(facts[0]);
  assert.deepEqual(posts[0], { path: "profiles/update", body: { profile_scope: "scope:A", profile_key: "favorite_food", value: "new value", category: "preference", confidence: .85, expires_at: null }, options: { retries: 0 } });
  peek.confirmed = false; await page.deleteItem("scope:B", "preferred_nickname"); assert.equal(posts.length, 1);
  peek.confirmed = true; await page.deleteItem("scope:B", "preferred_nickname");
  assert.deepEqual(posts[1].body, { profile_scope: "scope:B", profile_key: "preferred_nickname" });
});
test("a slow previous identity response cannot replace the new user's facts", async () => {
  let resolveA;
  const { page, state } = setup({ get(_path, params) { return params.identity === "A" ? new Promise(resolve => { resolveA = resolve; }) : Promise.resolve({ items: [facts[1]], total: 1 }); } });
  const first = page.selectUser({ ...user, identity: "A" });
  await page.selectUser({ ...user, identity: "B" }); resolveA({ items: [facts[0]], total: 1 }); await first;
  assert.equal(state.profile.identity, "B"); assert.equal(state.profile.items[0].profile_scope, "scope:B");
});
test("directory load failures remain visible and retryable", async () => {
  const { page, elements } = setup({ async get() { throw new Error("offline <script>"); } });
  await page.fetch(); assert.match(elements["profile-users"].innerHTML, /role="alert"/);
  assert.match(elements["profile-users"].innerHTML, /data-profile-retry="list"/);
  assert.match(elements["profile-users"].innerHTML, /offline &lt;script&gt;/);
});

test("cancelling profile deletion preserves the live editor node and unsaved input", async () => {
  let closes = 0;
  const { page, peek, elements } = setup({ async post() { throw new Error("cancel must not delete"); } });
  peek.close = () => { closes++; };
  const editor = elements["profile-editor-form"];
  elements["profile-edit-value"].value = "unsaved correction";
  peek.showConfirmDialog = async () => {
    elements["profile-editor-form"] = element(); // Reconstructed confirmation fallback loses listeners/draft.
    return false;
  };
  await page.deleteItem("scope:A", "favorite_food");
  assert.equal(closes, 0);
  assert.equal(elements["peek-body"].children?.[0], editor);
  assert.equal(elements["profile-edit-value"].value, "unsaved correction");
  assert.equal(page._deleting, false);
});
