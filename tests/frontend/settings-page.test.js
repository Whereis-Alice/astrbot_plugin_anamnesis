import test from "node:test";
import assert from "node:assert/strict";
import { SettingsPage } from "../../pages/dashboard/modules/settings-page.js";
const fields = [{ path: "user_profile.enabled", type: "bool", value: false }, { path: "retrieval.timeout", type: "float", value: 5, min: 0, max: 120 }, { path: "identity.sessions", type: "list", value: [] }];
function setup(api) {
  const elements = {};
  const el = () => ({ textContent: "", innerHTML: "", disabled: false, setAttribute() {}, addEventListener() {}, querySelectorAll: () => [], classList: { toggle() {} } });
  globalThis.window = { t: (k, ...args) => `${k}:${args.join("|")}`, getLanguage: () => "zh" };
  globalThis.document = { getElementById: id => elements[id] ||= el(), querySelectorAll: () => [] };
  const page = new SettingsPage({}, api); page.sections = [{ key: "profiles", title: "Profiles", fields: structuredClone(fields) }];
  return { page, elements };
}
function input(path, value, extra = {}) { return { value, dataset: { settingPath: path, ...extra.dataset }, checked: extra.checked, setAttribute() {}, closest: () => null }; }
test("settings preserve false, zero and structured types; only changed paths are posted", async () => {
  const posts = [], { page } = setup({ async post(path, body, options) { posts.push({ path, body, options }); return { config: {} }; } });
  page.change(input("user_profile.enabled", "", { checked: true }));
  page.change(input("retrieval.timeout", "0"));
  page.change(input("identity.sessions", '["chat:A"]', { dataset: { structured: "true" } }));
  await page.save();
  assert.deepEqual(posts, [{ path: "config/update", body: { updates: { "user_profile.enabled": true, "retrieval.timeout": 0, "identity.sessions": ["chat:A"] } }, options: { retries: 0 } }]);
  assert.equal(page.draft.size, 0);
});
test("invalid JSON and out-of-range numbers block a save; correcting restores the original", () => {
  const { page, elements } = setup({});
  page.change(input("identity.sessions", '{', { dataset: { structured: "true" } }));
  page.change(input("retrieval.timeout", "-1"));
  assert.equal(page.invalid.size, 2); assert.equal(elements["settings-save"].disabled, true);
  page.change(input("identity.sessions", '[]', { dataset: { structured: "true" } }));
  page.change(input("retrieval.timeout", "5"));
  assert.equal(page.invalid.size, 0); assert.equal(page.draft.size, 0);
});
test("a failed save retains the user's changes and displays an inline error", async () => {
  const { page, elements } = setup({ async post() { throw new Error("database busy <x>"); } });
  page.change(input("user_profile.enabled", "", { checked: true })); await page.save();
  assert.equal(page.draft.get("user_profile.enabled"), true);
  assert.match(elements["settings-feedback"].innerHTML, /database busy &lt;x&gt;/);
});
