import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const pagesDir = join(here, "../../pages");
const dashboardDir = join(pagesDir, "dashboard");
const i18nSource = readFileSync(join(dashboardDir, "i18n.js"), "utf-8");
const appSource = readFileSync(join(dashboardDir, "app.js"), "utf-8");
const indexHtml = readFileSync(join(dashboardDir, "index.html"), "utf-8");

const PREFIX = "anamnesis:";
const LANG_KEY = PREFIX + "lang";
const THEME_KEY = PREFIX + "theme";
/* 前端允许使用的全部 storage key；新增 key 必须同步更新这里。 */
const ALLOWED_KEYS = [LANG_KEY, THEME_KEY];

function setGlobal(name, value) {
  Object.defineProperty(globalThis, name, { value, writable: true, configurable: true });
}

/**
 * 轻量浏览器桩：只覆盖 i18n.js 载入期与 setLanguage 用到的 API，
 * 并记录所有 localStorage 读写，用于校验命名空间是否统一。
 */
function loadI18n(seed = {}) {
  const store = new Map(Object.entries(seed));
  const reads = [];
  const writes = [];

  setGlobal("localStorage", {
    getItem(key) {
      reads.push(key);
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      writes.push({ key, value: String(value) });
      store.set(key, String(value));
    },
    removeItem(key) {
      store.delete(key);
    },
  });
  setGlobal("window", {
    location: { search: "" },
    addEventListener() {},
    dispatchEvent() {},
  });
  setGlobal("document", {
    documentElement: { setAttribute() {}, getAttribute: () => "light" },
    addEventListener() {},
    querySelectorAll: () => [],
  });

  (0, eval)(i18nSource);
  return { store, reads, writes, win: globalThis.window };
}

/* 递归收集 pages/ 下的文本源文件（跳过 vendor 第三方产物）。 */
function collectSources(dir, acc = []) {
  for (const entry of readdirSync(dir)) {
    if (entry === "vendor") continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      collectSources(full, acc);
      continue;
    }
    if (!/\.(js|css|html|json|md)$/i.test(entry)) continue;
    acc.push([full, readFileSync(full, "utf-8")]);
  }
  return acc;
}

test("i18n.js exposes one frozen storage namespace", () => {
  const { win } = loadI18n();
  const ns = win.AnamStorage;

  assert.ok(ns, "i18n.js 必须导出 window.AnamStorage 作为唯一来源");
  assert.equal(ns.prefix, PREFIX);
  assert.equal(ns.langKey, LANG_KEY);
  assert.equal(ns.key("theme"), THEME_KEY);
  assert.ok(Object.isFrozen(ns), "命名空间应冻结，避免被其他脚本改写");
});

test("i18n.js only touches namespaced storage keys", () => {
  const { reads, writes, win } = loadI18n();
  win.setLanguage("en", { persist: true });

  assert.deepEqual(writes, [{ key: LANG_KEY, value: "en" }]);
  for (const key of reads.concat(writes.map((entry) => entry.key))) {
    assert.ok(key.startsWith(PREFIX), "storage key 缺少命名空间前缀: " + key);
    assert.ok(ALLOWED_KEYS.includes(key), "未登记的 storage key: " + key);
  }
});

test("a persisted language is read back from the namespaced key", () => {
  const { win } = loadI18n({ [LANG_KEY]: "ru" });
  assert.equal(win.getLanguage(), "ru");
});

test("app.js never hardcodes storage key literals", () => {
  const literalCalls = appSource.match(/localStorage\.(get|set|remove)Item\(\s*["'`]/g) || [];
  assert.deepEqual(literalCalls, [], "app.js 必须使用共享常量而不是字面量 key");
});

test("app.js storage fallback stays in sync with i18n.js", () => {
  const { win } = loadI18n();
  const ns = win.AnamStorage;
  const fallback = appSource.match(/window\.AnamStorage\s*\|\|\s*\{[\s\S]*?\};/);

  assert.ok(fallback, "app.js 需要保留 window.AnamStorage 兜底定义");
  const literals = [...fallback[0].matchAll(/["'`]([^"'`]*)["'`]/g)].map((m) => m[1]);
  assert.ok(literals.includes(ns.prefix), "兜底前缀与 i18n.js 不一致");
  assert.ok(literals.includes(ns.langKey), "兜底 lang key 与 i18n.js 不一致");
  for (const literal of literals) {
    assert.ok(
      literal === ns.prefix || literal === ns.langKey,
      "兜底块出现未知字面量: " + literal,
    );
  }
});

test("app.js derives every storage key from the shared namespace", () => {
  const { win } = loadI18n();
  const ns = win.AnamStorage;
  const derived = [...appSource.matchAll(/STORAGE\.key\(\s*["'`]([^"'`]+)["'`]\s*\)/g)]
    .map((m) => ns.key(m[1]));

  assert.ok(derived.includes(THEME_KEY), "主题 key 应由 STORAGE.key(\"theme\") 派生");
  for (const key of derived) {
    assert.ok(ALLOWED_KEYS.includes(key), "未登记的 storage key: " + key);
  }
  assert.match(appSource, /LANG_KEY\s*=\s*STORAGE\.langKey/);
});

test("pages/ keeps no upstream lmem namespace or brand residue", () => {
  const sources = collectSources(pagesDir);
  assert.ok(sources.length > 0, "未扫描到任何前端源文件");

  for (const [file, text] of sources) {
    /* selMemId 是图渲染里的局部变量（原本就含 lMem），区分大小写的替换不应命中它。 */
    const stripped = text.replaceAll("selMemId", "");
    assert.ok(!/lmem/i.test(stripped), "残留旧命名空间 lmem: " + file);
    assert.ok(!/livingmemory/i.test(stripped), "残留旧插件名: " + file);
    assert.ok(!/\bLiving\b/.test(stripped), "残留旧品牌文案: " + file);
  }
});

test("index.html renders the Anamnesis logotype", () => {
  assert.match(indexHtml, /<span class="brand-mark">AN<\/span>/);
  assert.match(indexHtml, /<strong>Anam<\/strong><strong>nesis<\/strong>/);
});
