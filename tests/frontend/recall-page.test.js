import assert from "node:assert/strict";
import test from "node:test";
import { RecallPage } from "../../pages/dashboard/modules/recall-page.js";

function element() {
  return { value: "", textContent: "", innerHTML: "", disabled: false, dataset: {},
    listeners: {}, classList: { add() {}, remove() {} },
    querySelectorAll() { return []; }, addEventListener(type, callback) { this.listeners[type] = callback; } };
}
function setup(api = {}) {
  const ids = ["recall-query", "recall-k", "recall-k-value", "recall-session", "recall-search-btn", "recall-results", "recall-stats", "recall-count-text", "recall-time-text", "recall-mode", "recall-persona", "recall-start-time", "recall-end-time"];
  const elements = Object.fromEntries(ids.map(id => [id, element()]));
  elements["recall-k"].value = "5";
  elements["recall-mode"].value = "auto";
  const labels = [];
  globalThis.document = { getElementById: id => elements[id] || null, querySelectorAll: () => labels };
  globalThis.window = { t: (key, ...args) => `${key}:${args.join(",")}`, getLanguage: () => "zh", addEventListener() {} };
  const state = {}, peek = { renderMemory() {} };
  return { page: new RecallPage(state, api, peek), state, elements, labels };
}

test("pure time recall sends raw exclusive boundary and selected persona without browser timezone conversion", async () => {
  const calls = [];
  const { page, elements } = setup({ async post(path, params) { calls.push({ path, params }); return { results: [] }; } });
  elements["recall-mode"].value = "time";
  elements["recall-session"].value = " scope ";
  elements["recall-persona"].value = " p ";
  elements["recall-start-time"].value = "2026-10-01T00:00:00+08:00";
  elements["recall-end-time"].value = "2026-10-02T00:00:00+08:00";
  await page.runRecall();
  assert.deepEqual(calls, [{ path: "recall/test", params: {
    query: "", k: 5, mode: "time", session_id: "scope", persona_id: "p",
    start_time: "2026-10-01T00:00:00+08:00", end_time: "2026-10-02T00:00:00+08:00" } }]);
});

test("empty semantic query is rejected even with a time field", async () => {
  const calls = [], { page, elements } = setup({ async post(...args) { calls.push(args); } });
  elements["recall-mode"].value = "semantic";
  elements["recall-start-time"].value = "100";
  await page.runRecall();
  assert.equal(calls.length, 0);
});

test("manual date fields are added once and explain next-midnight end boundaries", () => {
  const { page, elements } = setup();
  delete elements["recall-mode"];
  const inserts = [], form = { insertAdjacentHTML(_where, html) { inserts.push(html); elements["recall-mode"] = element(); } };
  elements["recall-query"].closest = () => form;
  page.ensureTemporalControls(); page.ensureTemporalControls();
  assert.equal(inserts.length, 1);
  assert.match(inserts[0], /for="recall-persona"/);
  assert.match(inserts[0], /结束时间（不包含）/);
  assert.match(inserts[0], /次日零点/);
  assert.match(inserts[0], /value="hybrid"/);
});

test("graph evidence expands safely and the resolved time basis remains visible", () => {
  const { page, elements } = setup();
  page.renderResults({ results: [{ memory_id: 1, content: "coffee", similarity_score: .5,
    metadata: { importance: .5, time_basis: "atom_event_time", persona_id: "p" },
    score_breakdown: { graph_evidence_reason: "weak_literal_source_support", graph_evidence_matched_tokens: ["<script>alert(1)</script>"] } }],
    retrieval: { mode: "time", time_range: { start: 100, end: 200, timezone: "UTC" } } }, 1);
  assert.match(elements["recall-results"].innerHTML, /事实事件时间/);
  assert.match(elements["recall-results"].innerHTML, /<details/);
  assert.match(elements["recall-results"].innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(elements["recall-results"].innerHTML, /<script>/);
  assert.match(elements["recall-time-text"].textContent, /UTC/);
});

test("locale refresh preserves entered boundaries and updates labels", () => {
  const { page, elements, labels } = setup();
  const label = element(); label.dataset.recallLabel = "end"; labels.push(label);
  elements["recall-end-time"].value = "2026-10-02T00:00:00+08:00";
  window.getLanguage = () => "en";
  page.updateTemporalLabels();
  assert.equal(label.textContent, "End time (exclusive)");
  assert.equal(elements["recall-end-time"].value, "2026-10-02T00:00:00+08:00");
});
