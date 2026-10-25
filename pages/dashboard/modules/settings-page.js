/** Schema-backed settings editor. Only changed paths are sent to the host. */
import { debounce, esc } from "./utils.js";
const COMMON = /(?:^|\.)(?:enabled|search_timeout_seconds|recall_timeout|recall_timeout_seconds|scope|scope_mode|granularity|provider_id|provider|maintenance_interval_hours|busy_timeout_ms)$|retrieval.*timeout|identity|user_profile\.enabled/;
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const atPath = (obj, path) => path.split(".").reduce((value, key) => value?.[key], obj);

export class SettingsPage {
  constructor(state, api) {
    this.state = state; this.api = api; this.sections = []; this.config = {};
    this.draft = new Map(); this.invalid = new Map(); this.rawInvalid = new Map(); this.query = ""; this.common = false; this.collapsed = new Set();
    this._generation = 0;
  }
  t(key, ...args) { return window.t?.(key, ...args) || key; }
  async fetch() {
    const generation = ++this._generation;
    this.feedback(this.t("common.loading"));
    try {
      const data = await this.api.get("config", { lang: window.getLanguage?.() || "zh" });
      if (generation !== this._generation) return;
      this.sections = Array.isArray(data.sections) ? data.sections : [];
      this.config = data.config || {};
      this.feedback(""); this.render();
    } catch (error) { if (generation === this._generation) this.feedback(error.message || this.t("settings.fetchFailed"), true, true); }
  }
  fields() { return this.sections.flatMap(section => section.fields || []); }
  value(field) { return this.draft.has(field.path) ? this.draft.get(field.path) : field.value ?? atPath(this.config, field.path) ?? field.default; }
  feedback(message, error = false, retry = false) {
    const el = document.getElementById("settings-feedback");
    if (!el) return;
    el.classList.toggle("hidden", !message); el.setAttribute("role", error ? "alert" : "status");
    el.innerHTML = esc(message) + (retry ? `<button class="btn btn-sm" id="settings-retry">${esc(this.t("common.retry"))}</button>` : "");
    document.getElementById("settings-retry")?.addEventListener("click", () => this.fetch());
  }
  control(field) {
    const value = this.value(field), path = esc(field.path);
    const textValue = this.rawInvalid.has(field.path) ? this.rawInvalid.get(field.path) : value;
    const attributes = `id="setting-${path}" data-setting-path="${path}"${this.saving ? " disabled" : ""} aria-describedby="hint-${path}"`;
    if (Array.isArray(field.options) && field.options.length) return `<select class="select input" ${attributes}>${field.options.map((option, index) => `<option value="${index}"${same(option, value) ? " selected" : ""}>${esc(Array.isArray(field.option_labels) ? field.option_labels[index] ?? option : field.option_labels?.[String(option)] ?? option)}</option>`).join("")}</select>`;
    if (field.type === "bool" || field.type === "boolean") return `<input class="settings-toggle" type="checkbox" ${attributes}${value ? " checked" : ""}>`;
    if (["int", "integer", "float", "number"].includes(field.type)) return `<input class="input" type="number" ${attributes} value="${esc(textValue ?? "")}" step="${esc(field.step ?? (["int", "integer"].includes(field.type) ? 1 : "any"))}"${field.min != null ? ` min="${esc(field.min)}"` : ""}${field.max != null ? ` max="${esc(field.max)}"` : ""}>`;
    const structured = ["list", "array", "object", "dict", "template_list"].includes(field.type) || Array.isArray(value) || (value && typeof value === "object");
    if (structured || field.type === "text" || String(value ?? "").length > 120) return `<textarea class="input textarea" rows="${structured ? 4 : 3}" ${attributes} data-structured="${Boolean(structured)}">${esc(this.rawInvalid.has(field.path) ? textValue : structured ? JSON.stringify(value ?? [], null, 2) : value ?? "")}</textarea>`;
    if (/secret|password/i.test(field.type || "")) return `<input class="input" type="password" autocomplete="new-password" ${attributes} value="" placeholder="${esc(this.t("settings.secretUnchanged"))}">`;
    return `<input class="input" type="text" ${attributes} value="${esc(value ?? "")}">`;
  }
  render() {
    const container = document.getElementById("settings-sections");
    if (!container) return;
    const q = this.query.toLocaleLowerCase();
    const matches = field => (!this.common || COMMON.test(field.path)) && (!q || `${field.path} ${field.label} ${field.hint}`.toLocaleLowerCase().includes(q));
    let count = 0;
    container.innerHTML = this.sections.map(section => {
      const fields = (section.fields || []).filter(matches);
      if (!fields.length) return ""; count += fields.length;
      return `<details class="settings-section" data-settings-section="${esc(section.key)}"${!this.collapsed.has(section.key) || q ? " open" : ""}><summary><span>${esc(section.title || section.key)}</span><span class="text-secondary">${fields.length}</span></summary>${section.hint ? `<p class="settings-section-hint">${esc(section.hint)}</p>` : ""}<div class="settings-fields">${fields.map(field => `<div class="settings-field${this.draft.has(field.path) ? " is-dirty" : ""}"><div class="settings-field-copy"><label for="setting-${esc(field.path)}">${esc(field.label || field.path)}</label><p id="hint-${esc(field.path)}">${esc(field.hint || "")}</p><details class="settings-field-key"><summary>${esc(this.t("settings.configKey"))}</summary><code>${esc(field.path)}</code></details></div><div class="settings-field-control">${this.control(field)}<span class="settings-field-error" data-setting-error="${esc(field.path)}" role="alert">${esc(this.invalid.get(field.path) || "")}</span></div></div>`).join("")}</div></details>`;
    }).join("") || `<div class="table-empty">${esc(this.t("settings.noMatch"))}</div>`;
    this.updateDirty(); window.lmHydrateIcons?.();
    container.querySelectorAll("[data-settings-section]").forEach(el => el.addEventListener("toggle", () => { if (el.open) this.collapsed.delete(el.dataset.settingsSection); else this.collapsed.add(el.dataset.settingsSection); }));
  }
  updateDirty() {
    const save = document.getElementById("settings-save");
    if (save) save.disabled = this.saving || !this.draft.size || this.invalid.size > 0;
    const dirty = document.getElementById("settings-dirty");
    if (dirty) dirty.textContent = this.draft.size ? this.t("settings.dirty", this.draft.size) : "";
  }
  change(input) {
    const field = this.fields().find(f => f.path === input.dataset.settingPath);
    if (!field) return;
    try {
      let value = input.value;
      if (Array.isArray(field.options) && field.options.length) value = field.options[Number(input.value)];
      else if (["bool", "boolean"].includes(field.type)) value = input.checked;
      else if (["int", "integer", "float", "number"].includes(field.type)) {
        value = Number(input.value);
        if (!input.value.trim() || !Number.isFinite(value) || (["int", "integer"].includes(field.type) && !Number.isInteger(value)) || (field.min != null && value < field.min) || (field.max != null && value > field.max)) throw new Error(this.t("settings.invalidNumber"));
      } else if (input.dataset.structured === "true") {
        value = JSON.parse(input.value);
        if ((["list", "array", "template_list"].includes(field.type) || Array.isArray(field.value)) && !Array.isArray(value)) throw new Error(this.t("settings.invalidArray"));
        if (["object", "dict"].includes(field.type) && (!value || Array.isArray(value) || typeof value !== "object")) throw new Error(this.t("settings.invalidJson"));
      }
      this.invalid.delete(field.path);
      this.rawInvalid.delete(field.path);
      const original = field.value ?? atPath(this.config, field.path) ?? field.default;
      if (same(value, original)) this.draft.delete(field.path); else this.draft.set(field.path, value);
    } catch (error) { this.invalid.set(field.path, error instanceof SyntaxError ? this.t("settings.invalidJson") : error.message); this.rawInvalid.set(field.path, input.value); }
    const errorEl = [...document.querySelectorAll("[data-setting-error]")].find(el => el.dataset.settingError === field.path);
    if (errorEl) errorEl.textContent = this.invalid.get(field.path) || "";
    input.setAttribute("aria-invalid", this.invalid.has(field.path) ? "true" : "false");
    input.closest(".settings-field")?.classList.toggle("is-dirty", this.draft.has(field.path)); this.updateDirty();
  }
  async save() {
    if (this.saving || !this.draft.size || this.invalid.size) return;
    this.saving = true; const updates = Object.fromEntries(this.draft); this.render();
    try {
      const data = await this.api.post("config/update", { updates }, { retries: 0 });
      this.config = data.config || this.config;
      this.fields().forEach(field => { if (Object.hasOwn(updates, field.path)) field.value = updates[field.path]; });
      this.draft.clear(); this.feedback(this.t("settings.savedReload"));
    } catch (error) { this.feedback(error.message || this.t("settings.saveFailed"), true); }
    finally { this.saving = false; this.render(); }
  }
  initEventListeners() {
    if (this._bound) return; this._bound = true;
    document.getElementById("settings-refresh")?.addEventListener("click", () => this.fetch());
    document.getElementById("settings-save")?.addEventListener("click", () => this.save());
    document.getElementById("settings-search")?.addEventListener("input", debounce(event => { this.query = event.target.value; this.render(); }, 150));
    document.getElementById("settings-common")?.addEventListener("click", event => { this.common = !this.common; event.currentTarget.setAttribute("aria-pressed", String(this.common)); this.render(); });
    document.getElementById("settings-sections")?.addEventListener("input", event => { if (event.target.dataset.settingPath) this.change(event.target); });
    document.getElementById("settings-sections")?.addEventListener("change", event => { if (event.target.dataset.settingPath) this.change(event.target); });
  }
}
