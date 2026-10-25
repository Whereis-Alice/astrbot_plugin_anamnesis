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
