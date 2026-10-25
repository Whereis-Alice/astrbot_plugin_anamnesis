/** Synthetic native dashboard checks. Requires installed Edge or CHROMIUM_EXECUTABLE_PATH. */
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE_PATH ? pathToFileURL(process.env.PLAYWRIGHT_MODULE_PATH).href : "playwright-core");

const output = process.env.DASHBOARD_ARTIFACT_DIR;
if (!output) throw Error("Set DASHBOARD_ARTIFACT_DIR to the task artifact directory");
mkdirSync(output, { recursive: true });
const server = spawn(process.env.PYTHON || "python", ["tests/browser/serve.py", "--port", "8768"], { stdio: "ignore", windowsHide: true });
const base = "http://127.0.0.1:8768";
for (let count = 0; count < 100; count++) { try { await fetch(base); break; } catch { await new Promise(resolve => setTimeout(resolve, 100)); } }
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
const errors = [], checks = [];
page.on("pageerror", error => errors.push(error.message));
try {
  await page.goto(base + "/tests/browser/profile-layout.html");
  await page.waitForFunction(() => window.fixtureReady === true);
  const frame = page.frames().find(frame => frame.parentFrame()); assert.ok(frame);
  await frame.evaluate(() => {
    const original = window.AstrBotPluginPage.apiGet;
    window.adminPosts = [];
    const records = [
      { id: "delete-operation", kind: "delete", status: "completed", created_at: 1792890000 },
      { id: "correction-operation", kind: "correction", status: "preview", created_at: 1792890000 },
      { id: "import-operation", kind: "import", status: "completed", created_at: 1792890000 },
      { id: "expired-correction", kind: "correction", status: "completed", expired: true, created_at: 1792890000 },
    ];
    window.AstrBotPluginPage.apiGet = async (path, params) => {
      if (path === "page/operations") return records;
      if (path === "page/projection") return [];
      if (path === "page/episodes") return [{ id: "episode", title: "周末讨论合作游戏", summary: "测试用户讨论了合作游戏与下次活动。".repeat(10), source_session_id: "qq:GroupMessage:1091576468", memory_ids: [7, 8], revision: 1 }];
      return original(path, params);
    };
    window.AstrBotPluginPage.apiPost = async (path, body) => {
      window.adminPosts.push({ path, body });
      if (path === "page/corrections/preview") return { operation_id: "correction-operation", old_content: "用户喜欢咖啡。", new_content: "用户已经改为喜欢茶。", session_id: "qq:GroupMessage:1091576468", persona_id: "测试人格" };
      if (path === "page/corrections/confirm") return { operation_id: "correction-operation", confirmation_token: "synthetic-single-use-token" };
      if (path === "page/corrections/execute") { records[1].status = "completed"; return { new_memory_id: 9 }; }
      if (path === "page/operations/remove") {
        const index = records.findIndex(record => record.id === body.operation_id);
        if (index < 0) throw Error("Missing operation");
        records.splice(index, 1);
        return { removed: true };
      }
      return {};
    };
  });
  await frame.locator('[data-page="administration"]').click();
  await frame.waitForFunction(() => document.querySelectorAll(".admin-record").length === 4);
  for (const width of [1024, 700, 390]) {
    await page.locator("#preview").evaluate((element, width) => { element.style.width = width + "px"; element.style.height = "700px"; }, width);
    await frame.locator("#page-administration").evaluate(element => element.scrollTop = 0);
    const overflow = await frame.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    assert.equal(overflow, false, `overflow at ${width}px`);
    await page.locator("#preview").screenshot({ path: join(output, `administration-${width}.png`) });
    checks.push(`PASS ${width}px: no horizontal overflow`);
  }
  await page.locator("#preview").evaluate(element => element.style.width = "1024px");
  await frame.locator("#admin-memory-id").fill("7");
  await frame.locator("#admin-instruction").fill("纠正为喜欢茶");
  await frame.locator("#admin-preview").click();
  await frame.locator("#admin-execute-plan").waitFor();
  await frame.locator("#admin-execute-plan").click();
  await frame.locator("#admin-confirm-cancel-btn").click();
  assert.equal(await frame.evaluate(() => window.adminPosts.filter(post => post.path === "page/corrections/confirm").length), 0);
  assert.match(await frame.locator("#admin-detail").innerText(), /喜欢茶/);
  checks.push("PASS cancelling confirmation preserves the preview and issues no token");
  await frame.locator("#admin-execute-plan").click();
  await frame.locator("#admin-confirm-ok-btn").click();
  await frame.waitForFunction(() => window.adminPosts.some(post => post.path === "page/corrections/execute"));
  assert.deepEqual(await frame.evaluate(() => window.adminPosts.filter(post => /corrections\/(confirm|execute)$/.test(post.path)).map(post => post.path)), ["page/corrections/confirm", "page/corrections/execute"]);
  checks.push("PASS human confirmation precedes token issue and execution");
  await frame.locator('[data-admin-action="purge"]').click();
  await frame.locator("#admin-confirm-cancel-btn").click();
  assert.equal(await frame.evaluate(() => window.adminPosts.filter(post => post.path === "page/operations/purge").length), 0);
  checks.push("PASS cancelling permanent purge performs no deletion");
  assert.equal(await frame.locator('[data-operation-id="expired-correction"][data-admin-action="rollback"]').count(), 0);
  await frame.locator('[data-operation-id="expired-correction"][data-admin-action="remove"]').click();
  await frame.waitForFunction(() => window.adminPosts.some(post => post.path === "page/operations/remove"));
  assert.deepEqual(await frame.evaluate(() => window.adminPosts.filter(post => post.path === "page/operations/remove").map(post => post.body.operation_id)), ["expired-correction"]);
  assert.equal(await frame.locator('[data-operation-id="expired-correction"]').count(), 0);
  assert.equal(await frame.locator('[data-operation-id="correction-operation"][data-admin-action="remove"]').count(), 0);
  checks.push("PASS expired correction cleanup uses the log endpoint while retained corrections stay protected");
  assert.deepEqual(errors, []);
  writeFileSync(join(output, "administration-checks.txt"), checks.join("\n") + "\n");
  console.log(checks.join("\n"));
} finally { await browser.close(); server.kill(); }
