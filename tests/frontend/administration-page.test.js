import assert from "node:assert/strict";
import test from "node:test";
import { AdministrationPage, parseMemoryIds } from "../../pages/dashboard/modules/administration-page.js";

function setup(api = {}) {
  const elements = {};
  globalThis.window = { getLanguage: () => "zh" };
  globalThis.document = { getElementById: id => elements[id] ||= { innerHTML: "", textContent: "", value: "", hidden: false, files: [], listeners: {},
    addEventListener(type, listener) { this.listeners[type] = listener; }, scrollIntoView() {} } };
  const peek = { async showConfirmDialog() { return false; } };
  const page = new AdministrationPage({ page: "administration" }, api, peek);
  page.confirm = async (...args) => peek.showConfirmDialog(...args);
  return { page, elements, peek };
}

test("memory ID parser rejects missing, non-integer and unsafe values", () => {
  setup(); assert.deepEqual(parseMemoryIds("1, 2，1"), [1, 2]);
  for (const input of ["", "true", "-1", "1.2", "9007199254740992"]) assert.throws(() => parseMemoryIds(input));
});
test("operation and episode content is escaped", () => {
  const { page, elements } = setup();
  page.records = [{ id: '<script>alert(1)</script>', kind: '<img src=x>', status: "completed", created_at: 1 }];
  page.episodes = [{ id: "e", title: '<script>alert(2)</script>', summary: "<img src=x>", memory_ids: [1], source_session_id: "a<b" }];
  page.render();
  assert.ok(!elements["admin-records"].innerHTML.includes("<script>"));
  assert.ok(!elements["admin-episodes"].innerHTML.includes("<img"));
  assert.match(elements["admin-episodes"].innerHTML, /&lt;script/);
});
test("cancelled human confirmation never obtains or executes a token", async () => {
  const posts = []; const { page, elements } = setup({ async post(path, body) { posts.push({ path, body }); } });
  await page.showPreview({ operation_id: "p", old_content: "before", new_content: "after" });
  await elements["admin-execute-plan"].listeners.click();
  assert.deepEqual(posts, []); assert.equal(elements["admin-detail"].hidden, false);
});
test("human confirmation precedes token issue and execution", async () => {
  const posts = []; const { page, elements, peek } = setup({ async post(path, body) { posts.push({ path, body }); return { operation_id: "p", confirmation_token: "server-token" }; } });
  peek.showConfirmDialog = async () => true; page.fetch = async () => {};
  await page.showPreview({ operation_id: "p", old_content: "before", new_content: "after" });
  await elements["admin-execute-plan"].listeners.click();
  assert.deepEqual(posts.map(post => post.path), ["corrections/confirm", "corrections/execute"]);
  assert.equal(posts[1].body.confirmation_token, "server-token");
});
test("failed request shows feedback and releases action lock", async () => {
  const { page, elements } = setup();
  await page.run(async () => { throw Error("version changed"); });
  assert.equal(page.busy, false); assert.equal(elements["admin-feedback"].textContent, "version changed");
});

test("expired completed correction exposes cleanup without rollback", () => {
  const { page, elements } = setup();
  page.records = [{ id: "expired", kind: "correction", status: "completed", expired: true, created_at: 1 }];
  page.render();
  assert.match(elements["admin-records"].innerHTML, /data-admin-action="remove"/);
  assert.doesNotMatch(elements["admin-records"].innerHTML, /data-admin-action="rollback"/);
});
test("unexpired completed correction retains recovery snapshot protection", () => {
  const { page, elements } = setup();
  page.records = [{ id: "retained", kind: "correction", status: "completed", expired: false, created_at: 1 }];
  page.render();
  assert.match(elements["admin-records"].innerHTML, /data-admin-action="rollback"/);
  assert.doesNotMatch(elements["admin-records"].innerHTML, /data-admin-action="remove"/);
});
test("interrupted uncertain correction offers evidence review and recheck", () => {
  const { page, elements } = setup();
  page.records = [{ id: "review", kind: "correction", status: "needs_review", expired: false, created_at: 1 }];
  page.render();
  assert.match(elements["admin-records"].innerHTML, /中断待核验/);
  assert.match(elements["admin-records"].innerHTML, /data-admin-action="rollback"/);
  assert.doesNotMatch(elements["admin-records"].innerHTML, /data-admin-action="remove"/);
});

test("cancelling event source review leaves the original event and sends no job", async () => {
  const posts = []; const { page } = setup({ async post(path, body) { posts.push({ path, body }); } });
  const original = { id: "event", memory_ids: [7, 8], revision: 3, title: "original", stale: true };
  page.episodes = [original];
  page.editEpisodeMembers = async episode => { assert.equal(episode, original); return null; };
  page.fetch = async () => { throw Error("cancellation should not refresh"); };
  await page.action("rebuild", "event");
  assert.deepEqual(posts, []);
  assert.deepEqual(page.episodes, [original]);
});

test("event rebuild uses reviewed members and the revision from when review opened", async () => {
  const posts = []; const { page } = setup({ async post(path, body) { posts.push({ path, body }); } });
  page.episodes = [{ id: "event", memory_ids: [7, 8], revision: 3 }];
  page.editEpisodeMembers = async episode => {
    // A concurrent refresh must not silently authorize overwriting a newer event.
    episode.revision = 4;
    return { memory_ids: [9, 8], use_llm: false, title: "reviewed event" };
  };
  page.fetch = async () => {};
  await page.action("rebuild", "event");
  assert.deepEqual(posts, [{ path: "jobs/create", body: { kind: "episode", payload: {
    memory_ids: [9, 8], use_llm: false, title: "reviewed event", episode_id: "event", expected_revision: 3,
  } } }]);
});

test("a rejected event rebuild reports the error without changing the original record", async () => {
  const { page, elements } = setup({ async post() { throw Error("event version changed"); } });
  const original = { id: "event", memory_ids: [7], revision: 3, title: "original", summary: "original summary" };
  page.episodes = [original];
  page.editEpisodeMembers = async () => ({ memory_ids: [9], use_llm: true, title: "" });
  await page.run(() => page.action("rebuild", "event"));
  assert.equal(elements["admin-feedback"].textContent, "event version changed");
  assert.deepEqual(page.episodes, [original]);
  assert.equal(page.busy, false);
});
