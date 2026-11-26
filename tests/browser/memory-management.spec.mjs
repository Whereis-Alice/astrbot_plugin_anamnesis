import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";
import { installRedesignFixture } from "./redesign-fixture.mjs";

function installMemoryFixture() {
  const group = "qq:GroupMessage:reading";
  const groups = [
    { id: group, group_id: "reading", platform_id: "qq", group_name: "河岸读书会", count: 21 },
    { id: "telegram:GroupMessage:reading", group_id: "reading", platform_id: "telegram", group_name: "Telegram 读书小组", count: 1 },
    { id: "qq:GroupMessage:walking", group_id: "walking", platform_id: "qq", group_name: "周末徒步", count: 1 },
  ];
  const memories = Array.from({ length: 23 }, (_, index) => {
    const origin = index < 21 ? group : groups[index - 20].id;
    return {
      id: index + 1,
      summary: index < 21 ? "读书记忆 " + (index + 1) : "其他群聊记忆 " + (index + 1),
      metadata: { session_id: origin, status: "active", importance: 0.5 },
    };
  });
  const bridge = window.AstrBotPluginPage;
  const get = bridge.apiGet;
  const post = bridge.apiPost;
  const calls = [];
  let phase = "running";
  let restored = false;
  window.memoryFixture = { calls, finish: () => { phase = "completed"; } };

  const operation = () => ({
    operation_id: "synthetic-delete",
    status: restored ? "restored" : phase,
    mode: "recoverable",
    total: 2,
    processed_count: phase === "completed" ? 2 : 1,
    deleted_count: phase === "completed" ? 2 : 1,
    failed_count: 0,
    failed_ids: [],
    can_restore: phase === "completed" && !restored,
  });

  bridge.apiGet = async (path, params = {}) => {
    calls.push({ method: "GET", path, params });
    if (path === "page/memories/groups") {
      const query = params.q || "";
      const matched = groups.filter(item => [item.group_id, item.group_name].some(text => text.includes(query)));
      return { items: matched, has_more: false };
    }
    if (path === "page/memories") {
      const size = Number(params.page_size);
      const offset = (Number(params.page) - 1) * size;
      const items = memories.filter(item => (!params.group || item.metadata.session_id === params.group) &&
        (!params.status || item.metadata.status === params.status));
      return { items: items.slice(offset, offset + size), total: items.length, has_more: offset + size < items.length };
    }
    if (path === "page/operations/delete/status") return operation();
    return get(path, params);
  };
  bridge.apiPost = async (path, body = {}) => {
    calls.push({ method: "POST", path, body });
    if (path === "page/memories/export") return { filename: "reading.json", content: "{}", memory_count: 21 };
    if (path === "page/operations/delete/start") return { ...operation(), status: "queued", processed_count: 0, deleted_count: 0 };
    if (path === "page/operations/restore") {
      restored = true;
      return { operation_id: "synthetic-delete", ids: [1, 21], conflicts: [], purged: false };
    }
    return post(path, body);
  };
}

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE_PATH
  ? pathToFileURL(process.env.PLAYWRIGHT_MODULE_PATH).href
  : "playwright-core");

const base = process.env.DASHBOARD_BASE_URL || "http://127.0.0.1:8797";
const output = process.env.DASHBOARD_ARTIFACT_DIR;
const server = process.env.DASHBOARD_BASE_URL ? null : spawn(
  process.env.PYTHON || "python", ["tests/browser/serve.py", "--port", "8797"],
  { stdio: "ignore", windowsHide: true },
);
let browser;
try {
  if (server) {
    for (let attempt = 0; attempt < 50; attempt++) {
      try {
        await fetch(base);
        break;
      } catch {
        await new Promise(resolve => setTimeout(resolve, 100));
      }
    }
  }

  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH ||
      (process.platform === "win32" ? "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" : undefined),
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 850 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript("(" + installRedesignFixture.toString() + ")();(" + installMemoryFixture.toString() + ")();");
  await page.goto(base + "/pages/dashboard/index.html");
  await page.locator('[data-page="memory"]').click();

  const groupPicker = page.locator("#mem-group-picker");
  const groupDialog = page.locator("#mem-group-dialog");
  await groupPicker.click();
  await groupDialog.locator("#mem-group-query").fill("河岸");
  await page.waitForFunction(() => window.memoryFixture.calls.some(call =>
    call.path === "page/memories/groups" && call.params.q === "河岸"));
  const groupResult = groupDialog.locator('[data-memory-group="qq:GroupMessage:reading"]');
  await groupResult.click();
  await page.waitForFunction(() => window.lmState.memory.total === 21);
  assert.match(await groupPicker.innerText(), /河岸读书会/);

  const exportButton = page.locator("#mem-export");
  await exportButton.locator("xpath=ancestor::details[1]/summary").click();
  assert.match(await exportButton.innerText(), /群/);
  const download = page.waitForEvent("download");
  await exportButton.click();
  assert.equal((await download).suggestedFilename(), "reading.json");
  assert.deepEqual(await page.evaluate(() => window.memoryFixture.calls.find(call =>
    call.path === "page/memories/export").body), { format: "json", group: "qq:GroupMessage:reading" });

  await page.locator('.memory-select[data-memory-id="1"]').check();
  await page.locator("#mem-next").click();
  await page.locator('.memory-select[data-memory-id="21"]').check();
  await page.locator("#mem-delete-selected").click();
  await page.locator("#mem-delete-dialog").waitFor();
  await page.keyboard.press("Escape");
  assert.equal(await page.evaluate(() => window.memoryFixture.calls.some(call =>
    call.path === "page/operations/delete/start")), false);

  await page.locator("#mem-delete-selected").click();
  await page.locator("#mem-delete-confirm").click();
  await page.waitForFunction(() => window.memoryFixture.calls.some(call =>
    call.path === "page/operations/delete/status" && call.params.operation_id === "synthetic-delete"));
  assert.deepEqual(await page.evaluate(() => window.memoryFixture.calls.find(call =>
    call.path === "page/operations/delete/start").body), { memory_ids: [1, 21], mode: "recoverable" });
  await page.locator("#mem-delete-progress").waitFor();
  assert.match(await page.locator("#mem-delete-status").innerText(), /1\s*(?:\/|of)\s*2/);
  assert.deepEqual(await page.locator("#mem-delete-meter").evaluate(meter => [meter.value, meter.max]), [1, 2]);
  if (output) {
    mkdirSync(output, { recursive: true });
    await page.screenshot({ path: join(output, "memory-delete-progress.png"), animations: "disabled" });
  }

  await page.evaluate(() => window.memoryFixture.finish());
  await page.locator("#mem-delete-undo").click();
  await page.waitForFunction(() => window.memoryFixture.calls.some(call =>
    call.path === "page/operations/restore" && call.body.operation_id === "synthetic-delete"));

  await page.waitForFunction(() => document.getElementById("mem-delete-status").textContent.includes("已恢复 2"));
  await page.locator('.memory-select[data-memory-id="21"]').check();
  await page.locator("#mem-delete-selected").click();
  await page.locator("#mem-delete-permanent").check();
  assert.match(await page.locator("#mem-delete-confirm").innerText(), /永久/);
  await page.keyboard.press("Escape");

  await page.setViewportSize({ width: 390, height: 750 });
  await groupPicker.click();
  await groupDialog.waitFor();
  const dialogBox = await groupDialog.boundingBox();
  assert.ok(dialogBox.x >= 0 && dialogBox.x + dialogBox.width <= 391);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
  if (output) await page.screenshot({ path: join(output, "memory-group-picker-mobile.png"), animations: "disabled" });
  await groupDialog.locator('[data-memory-group="telegram:GroupMessage:reading"]').click();
  await page.waitForFunction(() => window.lmState.memory.total === 1 && window.lmState.memory.selectedIds.size === 0);
  assert.match(await groupPicker.innerText(), /Telegram/);
  assert.deepEqual(errors, []);
  console.log("PASS group search, scoped export, cross-page selection, deletion progress, undo, permanent confirmation, and mobile group picker");
} finally {
  await browser?.close();
  server?.kill();
}
