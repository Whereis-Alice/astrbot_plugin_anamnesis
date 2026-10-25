/** Time controls must fit native Pages and retain server timezone boundaries. */
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE_PATH ? pathToFileURL(process.env.PLAYWRIGHT_MODULE_PATH).href : "playwright-core");
const output = process.env.DASHBOARD_ARTIFACT_DIR;
if (!output) throw Error("Set DASHBOARD_ARTIFACT_DIR");
mkdirSync(output, { recursive: true });
const server = spawn(process.env.PYTHON || "python", ["tests/browser/serve.py", "--port", "8770"], { stdio: "ignore", windowsHide: true });
const base = "http://127.0.0.1:8770";
for (let i = 0; i < 50; i++) { try { await fetch(base); break; } catch { await new Promise(resolve => setTimeout(resolve, 100)); } }
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
const errors = [], checks = [];
page.on("pageerror", error => errors.push(error.message));
try {
  await page.goto(base + "/tests/browser/profile-layout.html");
  await page.waitForFunction(() => window.fixtureReady === true);
  const frame = page.frames().find(frame => frame.parentFrame());
  await frame.locator('[data-page="recall"]').click();
  await frame.locator("#recall-mode").selectOption("time");
  await frame.locator("#recall-start-time").fill("2026-10-01T00:00:00");
  await frame.locator("#recall-end-time").fill("2026-10-02T00:00:00");
  await frame.locator("#recall-persona").fill("selected-persona");
  await frame.locator("#recall-session").fill("qq:GroupMessage:1");
  for (const width of [1024, 700, 390, 375]) {
    await page.locator("#preview").evaluate((element, width) => { element.style.width = width + "px"; element.style.height = "700px"; }, width);
    assert.equal(await frame.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `overflow at ${width}px`);
    assert.equal(await frame.locator("#recall-start-time").inputValue(), "2026-10-01T00:00:00");
    checks.push(`PASS ${width}px: time controls fit and preserve entered boundaries`);
  }
  await frame.locator("#recall-query").fill("");
  await frame.locator("#recall-search-btn").click();
  await frame.waitForFunction(() => window.profileFixturePosts.some(post => post.path === "page/recall/test"));
  const body = await frame.evaluate(() => window.profileFixturePosts.find(post => post.path === "page/recall/test").body);
  assert.equal(body.mode, "time"); assert.equal(body.persona_id, "selected-persona");
  assert.equal(body.start_time, "2026-10-01T00:00:00"); assert.equal(body.end_time, "2026-10-02T00:00:00");
  checks.push("PASS pure time query sends selected persona and unconverted exclusive end boundary");
  assert.deepEqual(errors, []);
  await page.locator("#preview").screenshot({ path: join(output, "recall-time-mobile.png") });
  writeFileSync(join(output, "recall-time-results.txt"), checks.join("\n") + "\n");
  console.log(checks.join("\n"));
} finally { await browser.close(); server.kill(); }
