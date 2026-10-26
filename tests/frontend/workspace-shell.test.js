import assert from "node:assert/strict";
import test from "node:test";
import { WorkspaceShell } from "../../pages/dashboard/modules/workspace-shell.js";

const storageKey = "anamnesis:sidebar-expanded";

function setup({ mobile = false, saved = null, denied = false } = {}) {
  const events = {}, frames = [], writes = [];
  const elements = {};
  function element(id) {
    const attributes = new Map(), classes = new Set();
    const node = {
      id, dataset: {}, textContent: "", hidden: false, isConnected: true, listeners: {}, children: [],
      addEventListener(type, handler) { this.listeners[type] = handler; },
      setAttribute(name, value) { attributes.set(name, String(value)); },
      getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
      hasAttribute(name) { return attributes.has(name); },
      removeAttribute(name) { attributes.delete(name); },
      toggleAttribute(name, enabled) { if (enabled) attributes.set(name, ""); else attributes.delete(name); },
      querySelector(selector) { return selector === "span" ? this.label : this.children.find(child => child.dataset.page); },
      querySelectorAll(selector) { return selector === ".nav-item[data-page]" ? this.children.filter(child => child.dataset.page) : this.children; },
      contains(child) { return this === child || this.children.includes(child); },
      getClientRects() { return this.hidden ? [] : [{}]; },
      closest() { return null; },
      focus() { document.activeElement = this; },
      classList: {
        contains: name => classes.has(name),
        toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
      },
    };
    elements[id] = node;
    return node;
  }
  for (const id of ["app", "workspace-sidebar", "workspace-main", "sidebar-toggle", "sidebar-close", "sidebar-backdrop", "focus-toggle", "focus-toggle-label", "workbench-page-title"]) element(id);
  const navItems = ["administration", "graph", "memory", "recall", "system", "prompts", "profiles", "settings"].map(page => {
    const item = element("nav-" + page);
    item.dataset.page = page;
    item.label = { textContent: page };
    return item;
  });
  elements["workspace-sidebar"].children = [elements["sidebar-close"], ...navItems];
  const media = { matches: mobile, addEventListener(type, handler) { this.listener = handler; } };
  globalThis.document = {
    activeElement: elements["sidebar-toggle"],
    getElementById: id => elements[id] || null,
    addEventListener(type, handler) { events[type] = handler; },
  };
  globalThis.window = {
    matchMedia: () => media,
    addEventListener(type, handler) { events[type] = handler; },
    requestAnimationFrame(callback) { frames.push(callback); return frames.length; },
    dispatchEvent(event) { events[event.type]?.(event); },
    t: key => key,
  };
  globalThis.localStorage = {
    getItem(key) { assert.equal(key, storageKey); if (denied) throw new Error("Storage denied"); return saved; },
    setItem(key, value) { if (denied) throw new Error("Storage denied"); writes.push([key, value]); saved = value; },
  };
  const shell = new WorkspaceShell(storageKey);
  shell.init();
  return { shell, elements, writes, events, media, navItems, frames };
}

function keyEvent(key, shiftKey = false) {
  return { key, shiftKey, prevented: false, stopped: false,
    preventDefault() { this.prevented = true; },
    stopImmediatePropagation() { this.stopped = true; },
  };
}

test("desktop rail remembers expansion and remains usable when sandbox storage is denied", () => {
  let fixture = setup();
  assert.equal(fixture.elements.app.classList.contains("sidebar-expanded"), false);
  fixture.elements["sidebar-toggle"].listeners.click();
  assert.equal(fixture.elements.app.classList.contains("sidebar-expanded"), true);
  assert.equal(fixture.elements["sidebar-toggle"].getAttribute("aria-expanded"), "true");
  assert.deepEqual(fixture.writes, [[storageKey, "true"]]);
  fixture = setup({ saved: "true" });
  assert.equal(fixture.elements.app.classList.contains("sidebar-expanded"), true);
  fixture.shell.toggleNavigation();
  assert.deepEqual(fixture.writes, [[storageKey, "false"]]);
  fixture = setup({ denied: true });
  assert.doesNotThrow(() => fixture.shell.toggleNavigation());
  assert.equal(fixture.elements.app.classList.contains("sidebar-expanded"), true);
});

test("mobile drawer contains focus, closes after every navigation, and preserves desktop preference", () => {
  const { shell, elements, writes, navItems } = setup({ mobile: true, saved: "true" });
  assert.equal(elements["workspace-sidebar"].hasAttribute("inert"), true);
  for (const nav of navItems) {
    elements["sidebar-toggle"].focus();
    shell.toggleNavigation();
    assert.equal(elements.app.classList.contains("mobile-nav-open"), true);
    assert.equal(elements["workspace-main"].hasAttribute("inert"), true);
    assert.equal(elements["workspace-sidebar"].getAttribute("aria-modal"), "true");
    assert.equal(document.activeElement, elements["sidebar-close"]);
    assert.equal(elements["sidebar-backdrop"].hidden, false);
    nav.focus();
    shell.setPage(nav.dataset.page);
    assert.equal(elements["workspace-main"].hasAttribute("inert"), false);
    assert.equal(elements["workspace-sidebar"].hasAttribute("inert"), true);
    assert.equal(elements["workspace-sidebar"].getAttribute("role"), null);
    assert.equal(elements["sidebar-backdrop"].hidden, true);
    assert.equal(document.activeElement, elements["sidebar-toggle"]);
    assert.equal(nav.getAttribute("aria-current"), "page");
    assert.equal(elements["workbench-page-title"].textContent, nav.dataset.page);
  }
  assert.equal(elements.app.classList.contains("sidebar-expanded"), true);
  assert.deepEqual(writes, []);
});

test("Escape closes only an open drawer, while Tab wraps inside the mobile navigation", () => {
  const { shell, elements, events, navItems } = setup({ mobile: true });
  shell.toggleNavigation();
  const backwards = keyEvent("Tab", true);
  events.keydown(backwards);
  assert.equal(backwards.prevented, true);
  assert.equal(document.activeElement, navItems.at(-1));
  const forwards = keyEvent("Tab");
  events.keydown(forwards);
  assert.equal(document.activeElement, elements["sidebar-close"]);
  const escape = keyEvent("Escape");
  events.keydown(escape);
  assert.equal(escape.prevented, true);
  assert.equal(escape.stopped, true);
  assert.equal(document.activeElement, elements["sidebar-toggle"]);
  for (const key of ["Escape", "f", "Enter", "Tab"]) {
    const event = keyEvent(key);
    events.keydown(event);
    assert.equal(event.prevented, false, key + " stays available to the current editor");
  }
});

test("reading mode has an explicit exit, keeps navigation preference, and is not persisted", () => {
  const { shell, elements, writes } = setup({ saved: "true" });
  elements["focus-toggle"].listeners.click();
  assert.equal(elements.app.classList.contains("reading-mode"), true);
  assert.equal(elements["workspace-sidebar"].hasAttribute("inert"), true);
  assert.equal(elements["focus-toggle"].getAttribute("aria-pressed"), "true");
  assert.equal(elements["focus-toggle-label"].textContent, "Exit focus");
  window.t = key => key === "shell.exitFocus" ? "退出专注" : key;
  shell.refreshLabels();
  assert.equal(elements["focus-toggle-label"].textContent, "退出专注");
  elements["focus-toggle"].listeners.click();
  assert.equal(elements.app.classList.contains("reading-mode"), false);
  assert.equal(elements.app.classList.contains("sidebar-expanded"), true);
  assert.equal(elements["workspace-sidebar"].hasAttribute("inert"), false);
  assert.equal(elements["focus-toggle"].getAttribute("aria-pressed"), "false");
  assert.deepEqual(writes, []);
});

test("changing viewport restores content and the desktop sidebar without leaving a backdrop", () => {
  const { shell, elements, media, frames } = setup({ mobile: true, saved: "true" });
  shell.toggleNavigation();
  media.listener({ matches: false });
  assert.equal(elements["workspace-main"].hasAttribute("inert"), false);
  assert.equal(elements["workspace-sidebar"].hasAttribute("inert"), false);
  assert.equal(elements["sidebar-backdrop"].hidden, true);
  assert.equal(elements.app.classList.contains("sidebar-expanded"), true);
  assert.equal(elements.app.classList.contains("mobile-nav-open"), false);
  media.listener({ matches: true });
  assert.equal(elements["workspace-sidebar"].hasAttribute("inert"), true);
  assert.equal(document.activeElement, elements["sidebar-toggle"]);
  assert.ok(frames.length, "layout changes notify the graph resize listener");
});
