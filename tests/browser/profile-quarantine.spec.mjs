/** Actual Chromium pause/resume UI against explicitly synthetic scoped records. */
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE_PATH ? pathToFileURL(process.env.PLAYWRIGHT_MODULE_PATH).href : "playwright-core");
const output = process.env.DASHBOARD_ARTIFACT_DIR;
if (!output) throw Error("Set DASHBOARD_ARTIFACT_DIR to a task artifact directory");
mkdirSync(output, { recursive: true });
const base = process.env.DASHBOARD_BASE_URL || "http://127.0.0.1:8777";
let server;
if (!process.env.DASHBOARD_BASE_URL) {
  server = spawn(process.env.PYTHON || "python", ["tests/browser/serve.py", "--port", "8777"], { stdio: "ignore", windowsHide: true });
  for (let attempt = 0; attempt < 100; attempt++) { try { await fetch(base); break; } catch { await new Promise(resolve => setTimeout(resolve, 100)); } }
}
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || (process.platform === "win32" ? "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" : undefined), headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
const errors = [], checks = [];
page.on("pageerror", error => errors.push(error.message));
try {
  await page.goto(base + "/tests/browser/profile-layout.html");
  await page.waitForFunction(() => window.fixtureReady === true);
  const frame = page.frames().find(item => item.parentFrame());
  assert.ok(frame);
  const card = frame.locator('.profile-fact-card').filter({ has: frame.locator('[data-profile-edit="0"]') });
  const original = await card.locator('.profile-fact-value').textContent();
  await card.locator('.profile-adoption-control summary').click();
  await card.locator('[data-profile-pause-reason]').fill('等待核实来源 <img>，保留原值');
  await card.locator('[data-profile-quarantine="pause"]').click();
  await frame.locator('#confirm-cancel-btn').click();
  assert.equal(await frame.evaluate(() => window.profileFixturePosts.filter(item => item.path === 'page/profiles/quarantine').length), 0);
  checks.push('PASS cancelling pause produces no API mutation');
  for (const width of [1280, 390, 375]) {
    await page.locator('#preview').evaluate((element, value) => { element.style.width = `${value}px`; }, width);
    await card.locator('[data-profile-quarantine="pause"]').click();
    assert.match(await frame.locator('#peek-body').textContent(), /保留原值、来源和历史/);
    await frame.waitForFunction(() => { const box = document.getElementById('peek-panel').getBoundingClientRect(); return box.left >= 0 && box.right <= innerWidth + 1; });
    await page.locator('#preview').screenshot({ path: join(output, `profile-pause-confirm-${width}.png`) });
    await frame.locator('#confirm-cancel-btn').click();
  }
  await page.locator('#preview').evaluate(element => { element.style.width = '1280px'; });
  await card.locator('[data-profile-quarantine="pause"]').click();
  await frame.locator('#confirm-ok-btn').click();
  await card.locator('[data-profile-quarantine="resume"]').waitFor();
  await frame.waitForFunction(() => document.getElementById('peek-panel').getBoundingClientRect().left >= innerWidth - 1);
  assert.equal(await card.locator('.profile-fact-value').textContent(), original);
  assert.match(await card.locator('.profile-quarantine-reason').textContent(), /等待核实来源 <img>/);
  assert.equal(await card.locator('.profile-quarantine-reason img').count(), 0);
  const pause = await frame.evaluate(() => window.profileFixturePosts.find(item => item.path === 'page/profiles/quarantine'));
  assert.deepEqual(pause.body, { profile_scope: 'profile:alpha:session:A', profile_key: 'preferred_nickname', current_claim_id: 500, expected_revision: 3, action: 'pause', reason: '等待核实来源 <img>，保留原值' });
  assert.match(await card.locator('.profile-lifecycle').textContent(), /暂停采用/);
  await card.locator('.profile-adoption-history summary').click();
  assert.match(await card.locator('.profile-adoption-history').textContent(), /webui:fixture-admin/);
  await page.locator('#preview').screenshot({ path: join(output, 'profile-paused-desktop.png') });
  checks.push('PASS pause retains original value/evidence, displays escaped reason and administrator history');
  await card.locator('[data-profile-edit="0"]').click();
  assert.match(await frame.locator('#peek-body').textContent(), /暂停采用/);
  await frame.locator('#profile-edit-label').fill('人工编辑仍保留暂停');
  await frame.locator('#profile-editor-save').click();
  await card.locator('h5').filter({ hasText: '人工编辑仍保留暂停' }).waitFor();
  assert.equal(await card.locator('.profile-fact-value').textContent(), original);
  assert.equal(await card.locator('[data-profile-quarantine="resume"]').count(), 1);
  checks.push('PASS ordinary title editing retains the paused state');
  await page.locator('#preview').evaluate(element => { element.style.width = '375px'; });
  await frame.waitForFunction(() => document.getElementById('peek-panel').getBoundingClientRect().left >= innerWidth - 1);
  await card.locator('.profile-adoption-control').scrollIntoViewIfNeeded();
  await frame.waitForFunction(() => !document.getElementById('profile-reader').classList.contains('hidden'));
  await page.locator('#preview').screenshot({ path: join(output, 'profile-paused-mobile.png') });
  assert.equal(await frame.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  await card.locator('[data-profile-quarantine="resume"]').click();
  assert.match(await frame.locator('#peek-body').textContent(), /恢复不代表自动核实真假/);
  await frame.locator('#confirm-ok-btn').click();
  await card.locator('[data-profile-quarantine="pause"]').waitFor({ state: 'attached' });
  assert.equal(await card.locator('.profile-fact-value').textContent(), original);
  const resume = await frame.evaluate(() => window.profileFixturePosts.filter(item => item.path === 'page/profiles/quarantine').at(-1));
  assert.equal(resume.body.action, 'resume');
  assert.equal(resume.body.expected_revision, 5);
  assert.equal(resume.body.current_claim_id, 501);
  assert.equal(resume.body.reason, '');
  assert.deepEqual(errors, []);
  checks.push('PASS distinct resume action confirms truth caveat and uses the latest claim/revision at 375px');
  writeFileSync(join(output, 'profile-quarantine-results.txt'), checks.join('\n') + '\n');
  console.log(checks.join('\n'));
  console.log(`Artifacts: ${output}`);
} finally { await page.close(); await browser.close(); server?.kill(); }
