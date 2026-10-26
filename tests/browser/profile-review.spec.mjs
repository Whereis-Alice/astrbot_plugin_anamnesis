/** Real Chromium profile-review/alias interactions against synthetic records. */
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE_PATH ? pathToFileURL(process.env.PLAYWRIGHT_MODULE_PATH).href : "playwright-core");
const output = process.env.DASHBOARD_ARTIFACT_DIR;
if (!output) throw Error("Set DASHBOARD_ARTIFACT_DIR to a task artifact directory");
mkdirSync(output, { recursive: true });
let server;
const base = process.env.DASHBOARD_BASE_URL || "http://127.0.0.1:8769";
if (!process.env.DASHBOARD_BASE_URL) {
  server = spawn(process.env.PYTHON || "python", ["tests/browser/serve.py", "--port", "8769"], { stdio: "ignore", windowsHide: true });
  for (let attempt = 0; attempt < 100; attempt++) { try { await fetch(base); break; } catch { await new Promise(resolve => setTimeout(resolve, 100)); } }
}
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || (process.platform === "win32" ? "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" : undefined), headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
const errors = [], checks = [];
page.on("pageerror", error => errors.push(error.message));
try {
  await page.goto(base + "/tests/browser/profile-layout.html");
  await page.waitForFunction(() => window.fixtureReady === true);
  const frame = page.frames().find(item => item.parentFrame()); assert.ok(frame);
  assert.equal(await page.evaluate(() => window.fixtureInitialDetailReads), 0);
  await frame.locator('#profile-person-close').click();
  await frame.waitForFunction(() => document.getElementById('profile-reader').classList.contains('hidden'));
  for (const width of [1280, 1024, 390]) {
    await page.locator('#preview').evaluate((element, value) => { element.style.width = `${value}px`; }, width);
    const card = frame.locator('.profile-user').first();
    const readable = await card.evaluate(element => {
      const bounds = element.getBoundingClientRect();
      const previews = [...element.querySelectorAll('.profile-preview-value')];
      return bounds.width > 220 && previews.every(preview => {
        const box = preview.getBoundingClientRect();
        return box.width > 150 && box.height > 0 && preview.textContent.trim().length > 0 && preview.scrollWidth <= preview.clientWidth + 1;
      });
    });
    assert.ok(readable, `contact previews must use readable space without overflow at ${width}px`);
    assert.equal(await card.locator('.profile-preview-value').count(), 2);
    assert.equal(await frame.locator('.profile-user').count(), 3);
    assert.equal(await frame.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    if (width === 1280 || width === 390) await page.locator('#preview').screenshot({ path: join(output, `profile-contacts-${width}.png`) });
  }
  await page.locator('#preview').evaluate(element => { element.style.width = '1280px'; });
  if (await frame.locator('#profile-person-close').isVisible()) await frame.locator('#profile-person-close').click();
  await frame.locator('[data-profile-user="0"]').click();
  await frame.waitForFunction(() => !document.getElementById('profile-reader').classList.contains('hidden'));
  checks.push('PASS contacts show readable real previews and load full profiles only on click');
  const proposal = frame.locator('[data-profile-review="accept"]').first();
  await proposal.click();
  assert.equal(await frame.locator('#peek-panel').getAttribute('aria-hidden'), 'false');
  assert.equal(await frame.locator('#peek-panel').getAttribute('inert'), null);
  await frame.locator('#confirm-cancel-btn').click();
  await frame.waitForFunction(() => document.getElementById('peek-panel').getAttribute('aria-hidden') === 'true');
  assert.equal(await frame.evaluate(() => window.profileFixturePosts.filter(item => item.path === 'page/profiles/review').length), 0);
  checks.push('PASS profile review is visible and cancelling performs no mutation');

  for (const width of [1024, 700, 390, 375]) {
    await page.locator('#preview').evaluate((element, value) => { element.style.width = `${value}px`; }, width);
    await proposal.click();
    await frame.waitForFunction(() => {
      const panel = document.getElementById('peek-panel').getBoundingClientRect();
      return panel.left >= 0 && panel.right <= innerWidth + 1;
    });
    for (const id of ['confirm-cancel-btn', 'confirm-ok-btn']) {
      const bounds = await frame.locator('#' + id).evaluate(element => {
        const box = element.getBoundingClientRect();
        return { left: box.left, right: box.right, width: innerWidth };
      });
      assert.ok(bounds.left >= 0 && bounds.right <= bounds.width + 1, `${id} is clipped at ${width}px`);
    }
    if (width === 375) await page.locator('#preview').screenshot({ path: join(output, 'profile-review-mobile.png') });
    await frame.locator('#confirm-cancel-btn').click();
    await frame.waitForFunction(() => document.getElementById('peek-panel').getAttribute('aria-hidden') === 'true');
  }
  await page.locator('#preview').evaluate(element => { element.style.width = '1280px'; });
  checks.push('PASS review actions remain fully visible at 375–1024px');

  await proposal.click();
  await frame.waitForFunction(() => document.getElementById('peek-panel').getBoundingClientRect().right <= innerWidth + 1);
  await page.locator('#preview').screenshot({ path: join(output, 'profile-review-confirm.png') });
  await frame.locator('#confirm-ok-btn').click();
  await frame.waitForFunction(() => window.profileFixturePosts.some(item => item.path === 'page/profiles/review'));
  const review = await frame.evaluate(() => window.profileFixturePosts.find(item => item.path === 'page/profiles/review'));
  assert.deepEqual(review.body, { profile_scope: 'profile:alpha:session:B', profile_key: 'favorite_game_1', action: 'accept', claim_id: 200, expected_revision: 3 });
  await frame.locator('.profile-fact-value').filter({ hasText: '现在也喜欢一起看纪录片' }).waitFor();
  assert.equal(await frame.locator('.profile-proposals').count(), 0);
  checks.push('PASS accepted proposal sends exact scope/key/claim/revision and refreshes current value');

  await frame.locator('#profile-reader-tools > summary').click();
  await frame.locator('#profile-alias').fill('管理员备注：alpha <img>');
  await frame.locator('#profile-alias-save').click();
  await frame.waitForFunction(() => document.querySelector('#profile-reader h2')?.textContent === '管理员备注：alpha <img>');
  const alias = await frame.evaluate(() => window.profileFixturePosts.find(item => item.path === 'page/profiles/alias'));
  assert.deepEqual(alias.body, { identity: 'opaque:qq:alpha', alias: '管理员备注：alpha <img>' });
  assert.equal(await frame.locator('#profile-reader h2 img').count(), 0);
  if (await frame.locator('#profile-person-close').isVisible()) await frame.locator('#profile-person-close').click();
  await frame.locator('[data-profile-user="1"]').click();
  await frame.waitForFunction(() => document.querySelector('#profile-reader h2')?.textContent === '测试用户 beta');
  assert.equal(await frame.locator('#profile-alias').inputValue(), '');
  checks.push('PASS administrator alias is escaped and affects only the selected identity');

  if (await frame.locator('#profile-person-close').isVisible()) await frame.locator('#profile-person-close').click();
  await frame.locator('[data-profile-user="0"]').click();
  await frame.locator('[data-profile-edit="1"]').click();
  assert.equal(await frame.locator('#profile-edit-label').inputValue(), '喜欢的游戏');
  await frame.locator('#profile-edit-label').fill('我常玩的游戏');
  await frame.locator('#profile-edit-value').fill('人工确认后的值');
  await page.locator('#preview').screenshot({ path: join(output, 'profile-title-editor.png') });
  await frame.locator('#profile-editor-save').click();
  await frame.waitForFunction(() => window.profileFixturePosts.some(item => item.path === 'page/profiles/update'));
  const update = await frame.evaluate(() => window.profileFixturePosts.find(item => item.path === 'page/profiles/update'));
  assert.equal(update.body.expected_revision, 4);
  assert.equal(update.body.profile_scope, 'profile:alpha:session:B');
  assert.equal(update.body.profile_key, 'favorite_game_1');
  assert.equal(update.body.display_label, '我常玩的游戏');
  await frame.locator('.profile-fact-heading h5').filter({ hasText: '我常玩的游戏' }).waitFor();
  checks.push('PASS editable profile titles save exact scope/key/revision and refresh the heading');
  assert.deepEqual(errors, []);
  writeFileSync(join(output, 'profile-review-results.txt'), checks.join('\n') + '\n');
  console.log(checks.join('\n')); console.log(`Artifacts: ${output}`);
} finally { await page.close(); await browser.close(); server?.kill(); }
