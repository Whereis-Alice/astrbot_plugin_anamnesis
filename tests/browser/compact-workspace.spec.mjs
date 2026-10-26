/** Short-window regression against real layout, with synthetic Bridge data only. */
import assert from 'node:assert/strict';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {installRedesignFixture} from './redesign-fixture.mjs';

const {chromium} = await import(process.env.PLAYWRIGHT_MODULE_PATH
  ? pathToFileURL(process.env.PLAYWRIGHT_MODULE_PATH).href : 'playwright-core');
const output = process.env.DASHBOARD_ARTIFACT_DIR;
if (!output) throw Error('Set DASHBOARD_ARTIFACT_DIR to a task artifact directory');
mkdirSync(output, {recursive: true});
const base = process.env.DASHBOARD_BASE_URL || 'http://127.0.0.1:8780';
let server, browser;
const checks = [], failures = [];

async function inViewport(page, selector, {pointer = true} = {}) {
  const control = page.locator(selector).first();
  await control.scrollIntoViewIfNeeded();
  if (pointer && !await control.isDisabled()) await control.click({trial: true});
  const box = await control.boundingBox(), viewport = page.viewportSize();
  assert.ok(box && box.x >= -1 && box.y >= -1
    && box.x + box.width <= viewport.width + 1
    && box.y + box.height <= viewport.height + 1,
  `${selector} is clipped: ${JSON.stringify(box)}`);
  return control;
}

async function openNavigation(page) {
  if (page.viewportSize().width <= 760) await page.locator('#sidebar-toggle').click();
}

async function navigate(page, name) {
  await openNavigation(page);
  await page.locator(`.nav-item[data-page="${name}"]`).click();
  await page.waitForFunction(name => document.querySelector('.page.active')?.id === 'page-' + name, name);
}

async function noOverflow(page) {
  const metric = await page.evaluate(() => ({width: innerWidth,
    root: document.documentElement.scrollWidth, body: document.body.scrollWidth}));
  assert.ok(metric.root <= metric.width + 1 && metric.body <= metric.width + 1,
    `Horizontal document overflow: ${JSON.stringify(metric)}`);
}

try {
  if (!process.env.DASHBOARD_BASE_URL) {
    server = spawn(process.env.PYTHON || 'python', ['tests/browser/serve.py', '--port', '8780'],
      {stdio: 'ignore', windowsHide: true});
    server.on('error', error => { throw error; });
    for (let i = 0; i < 100; i++) {
      try { await fetch(base); break; }
      catch { await new Promise(resolve => setTimeout(resolve, 100)); }
    }
  }
  browser = await chromium.launch({headless: true, executablePath:
    process.env.CHROMIUM_EXECUTABLE_PATH || (process.platform === 'win32'
      ? 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' : undefined)});

  for (const [width, height] of [[900, 500], [390, 480], [740, 360], [740, 280]]) {
    for (const [lang, theme] of [['zh', 'light'], ['ru', 'dark']]) {
      const label = `${width}x${height}-${lang}-${theme}`;
      const context = await browser.newContext({viewport: {width, height}, reducedMotion: 'reduce'});
      await context.addInitScript(installRedesignFixture);
      const page = await context.newPage(), errors = [];
      page.setDefaultTimeout(8000);
      page.on('pageerror', error => errors.push(error.message));
      try {
        await page.goto(`${base}/pages/dashboard/index.html?theme=${theme}`);
        await page.waitForFunction(() => document.getElementById('gs-total')?.textContent === '1286');
        await page.evaluate(() => document.fonts.ready);

        // Manual theme choice must survive a reload even when the host disagrees.
        const selectedTheme = theme === 'light' ? 'dark' : 'light';
        await page.locator('#theme-toggle').click();
        assert.equal(await page.locator('html').getAttribute('data-theme'), selectedTheme);
        await page.reload();
        await page.waitForFunction(() => document.getElementById('gs-total')?.textContent === '1286');
        assert.equal(await page.locator('html').getAttribute('data-theme'), selectedTheme);
        await page.locator('#theme-toggle').click();

        // The footer must not cover the last navigation item; language stays usable.
        await openNavigation(page);
        await (await inViewport(page, '#lang-toggle')).click();
        await (await inViewport(page, `.lang-option[data-lang="${lang}"]`)).click();
        assert.equal(await page.locator('#lang-menu').getAttribute('open'), null);
        await page.waitForFunction(lang => window.getLanguage() === lang, lang);
        await inViewport(page, '.nav-item[data-page="settings"]');
        const nav = await page.locator('.sidebar-nav').evaluate(element => ({
          client: element.clientHeight, scroll: element.scrollHeight, top: element.scrollTop,
        }));
        assert.ok(nav.scroll > nav.client && nav.top > 0, 'short navigation scrolls to Settings');
        await page.screenshot({path: join(output, `compact-nav-${label}.png`), animations: 'disabled'});
        await page.locator('.nav-item[data-page="settings"]').click();
        await page.locator('#page-settings.active .settings-field').first().waitFor();
        if (width <= 760) assert.equal(await page.locator('#app').evaluate(
          element => element.classList.contains('mobile-nav-open')), false);

        await navigate(page, 'memory');
        await page.locator('.memory-summary-text').first().waitFor();
        for (const id of ['mem-toolbar-filters', 'mem-toolbar-transfer', 'mem-toolbar-selection']) {
          await page.locator(`#${id} > summary`).click();
        }
        assert.equal(await page.locator('.memory-toolbar details[open]').count(), 3);
        for (const selector of ['#mem-session', '#mem-sort', '#mem-page-size',
          '#mem-transfer-format', '#mem-export', '#mem-import', '#mem-batch-edit', '#mem-delete-selected']) {
          await inViewport(page, selector);
        }
        const tools = await page.locator('.memory-toolbar').evaluate(element => ({
          height: element.clientHeight, scroll: element.scrollHeight, top: element.scrollTop,
        }));
        assert.ok(tools.scroll > tools.height && tools.top > 0, 'expanded tools scroll internally');
        const table = await page.locator('#memories-scroll').boundingBox();
        assert.ok(table.height >= 80, `memory list retains a readable height: ${table.height}`);
        const pagination = page.locator('#page-memory .pagination');
        // Below 320px the whole page may scroll; pagination must still be reachable.
        if (height <= 320) await pagination.scrollIntoViewIfNeeded();
        const box = await pagination.boundingBox();
        assert.ok(box.y >= 0 && box.y + box.height <= height + 1,
          `pagination is clipped: ${JSON.stringify(box)}`);
        await noOverflow(page);
        await page.screenshot({path: join(output, `compact-tools-${label}.png`), animations: 'disabled'});

        await navigate(page, 'graph');
        await page.waitForFunction(() => window.Graph2D?.getDiagnostics().renderedNodes > 0);
        await page.locator('.graph-toolbar details > summary').click();
        await inViewport(page, '#graph-session-filter');
        await inViewport(page, '#graph-memory-id');
        await inViewport(page, '#graph-focus-btn');
        await inViewport(page, '#graph-canvas canvas', {pointer: false});
        await inViewport(page, '#graph-legend', {pointer: false});
        await noOverflow(page);
        await page.screenshot({path: join(output, `compact-graph-${label}.png`), animations: 'disabled'});
        assert.deepEqual(errors, [], 'no JavaScript errors');
        checks.push(`PASS ${label}: navigation, language, theme persistence, expanded tools, memory list, pagination, graph`);
      } catch (error) {
        failures.push(`${label}: ${error.message}`);
        await page.screenshot({path: join(output, `FAIL-compact-${label}.png`), animations: 'disabled'});
      } finally {
        await context.close();
      }
      console.log(`Completed ${label}; ${failures.length} failure(s)`);
    }
  }
} finally {
  writeFileSync(join(output, 'compact-results.json'), JSON.stringify({checks, failures}, null, 2) + '\n');
  await browser?.close();
  server?.kill();
}
console.log(checks.join('\n'));
assert.deepEqual(failures, [], failures.join('\n'));
