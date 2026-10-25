/** Identity groups presentation only; every mutation uses persisted scope/key. */
import { debounce, esc } from "./utils.js";
const CATEGORIES = ["identity", "preference", "status", "task_preference", "constraint"];
const KEY_LABELS = { preferred_nickname: "nickname", nickname: "nickname", preferred_name: "nickname", name: "name", real_name: "name", occupation: "occupation", profession: "occupation", age: "age", birthday: "birthday", location: "location", city: "location", favorite_food: "food", food_preference: "food", dietary_preference: "food", favorite_game: "game", favorite_games: "game", hobby: "hobby", hobbies: "hobby", language: "language", preferred_language: "language", communication_style: "style", response_style: "style", timezone: "timezone", current_status: "status" };

export class ProfilePage {
  constructor(state, apiClient, peekPanel) {
    this.state = state; this.api = apiClient; this.peek = peekPanel;
    state.profile = { users: [], items: [], total: 0, limit: 30, offset: 0, query: "", sessionId: "", identity: "", user: null, hasMore: false, detailTotal: 0, detailHasMore: false, category: "", lifecycle: "", profileEnabled: null, scopeMode: "", listError: "", detailError: "" };
    this._fetchGeneration = 0; this._detailGeneration = 0;
  }
  t(key, ...args) { return window.t?.(key, ...args) || key; }
  _hydrate() { window.lmHydrateIcons?.(); }
  _showToast(message, error = false) { window.lmShowToast?.(message, error); }

  async fetch() {
    const generation = ++this._fetchGeneration;
    const p = this.state.profile;
    const params = { limit: String(p.limit), offset: String(p.offset) };
    if (p.query) params.q = p.query;
    if (p.sessionId) params.session_id = p.sessionId;
    const refresh = document.getElementById("profile-refresh");
    if (refresh) refresh.disabled = true;
    try {
      const data = await this.api.get("profiles/users", params);
      if (generation !== this._fetchGeneration) return;
      p.users = Array.isArray(data.users) ? data.users : [];
      p.total = Number(data.total) || 0; p.limit = Number(data.limit) || p.limit;
      p.offset = Number(data.offset) || 0; p.hasMore = Boolean(data.has_more);
      p.profileEnabled = data.profile_enabled; p.scopeMode = data.scope_mode || ""; p.listError = "";
      if (!p.users.length && p.offset > 0 && p.total > 0) { p.offset = Math.max(0, Math.floor((p.total - 1) / p.limit) * p.limit); return this.fetch(); }
      const user = p.users.find(u => u.identity === p.identity) || p.users[0];
      this.renderDirectory();
      if (user) await this.selectUser(user);
      else { ++this._detailGeneration; p.identity = ""; p.user = null; p.items = []; this.renderDetail(); }
    } catch (error) { if (generation === this._fetchGeneration) { p.listError = error.message || this.t("profile.fetchFailed"); this.renderDirectory(); } }
    finally { if (generation === this._fetchGeneration && refresh) refresh.disabled = false; }
  }
  async selectUser(user, reveal = false) {
    const p = this.state.profile;
    p.identity = user.identity; p.user = user; p.items = []; p.detailTotal = Number(user.fact_count) || 0;
    p.detailHasMore = false; p.detailError = "";
    this.renderDirectory(); await this.fetchDetail(false);
    if (reveal && window.matchMedia?.("(max-width: 768px)").matches) document.getElementById("profile-reader")?.scrollIntoView({ block: "start", behavior: "smooth" });
  }
  async fetchDetail(append = false) {
    const p = this.state.profile;
    if (!p.identity) return;
    const generation = ++this._detailGeneration;
    const identity = p.identity;
    p.detailLoading = true; p.detailError = ""; this.renderDetail();
    try {
      const data = await this.api.get("profiles", { identity, limit: "500", offset: String(append ? p.items.length : 0) });
      if (generation !== this._detailGeneration || identity !== p.identity) return;
      const incoming = (Array.isArray(data.items) ? data.items : []).map(item => ({ ...item, profile_scope: item.profile_scope || item.scope || "", profile_key: item.profile_key || item.key || "", value: item.value == null ? "" : String(item.value) }));
      p.items = append ? [...p.items, ...incoming] : incoming;
      p.detailTotal = Number(data.total) || p.items.length;
      p.detailHasMore = Boolean(data.has_more) && incoming.length > 0;
    } catch (error) { if (generation === this._detailGeneration) p.detailError = error.message || this.t("profile.fetchFailed"); }
    finally { if (generation === this._detailGeneration) { p.detailLoading = false; this.renderDetail(); if (!append) { const reader = document.getElementById("profile-reader"); if (reader) reader.scrollTop = 0; } } }
  }
  lifecycle(item, now = Date.now() / 1000) {
    if (item.suppressed) return "suppressed";
    const expires = Number(item.expires_at);
    return expires > 0 ? expires <= now ? "expired" : "temporary" : "stable";
  }
  label(item) {
    if (item.display_label) return String(item.display_label);
    const key = item.profile_key || "";
    return KEY_LABELS[key] ? this.t("profile.keyLabel." + KEY_LABELS[key]) : key.replace(/[_:.-]+/g, " ").trim() || this.t("profile.fact");
  }
  _formatTime(value) { if (!value) return this.t("table.na"); const n = Number(value); return Number.isFinite(n) && n > 0 ? new Date(n < 1e11 ? n * 1000 : n).toLocaleString() : String(value); }
  _formatConfidence(value) { return value != null && value !== "" && Number.isFinite(Number(value)) ? Math.round(Math.min(1, Math.max(0, Number(value))) * 100) + "%" : this.t("table.na"); }
  _chatName(item) { return item.source_group_name || (item.source_group_id ? this.t("profile.groupWithId", item.source_group_id) : this.t("profile.privateOrUnknown")); }
  render() { this.renderDirectory(); this.renderDetail(); }

  renderDirectory() {
    const p = this.state.profile;
    const status = document.getElementById("profile-status");
    if (status) status.textContent = p.profileEnabled === false ? this.t("profile.disabled") : p.profileEnabled === true ? this.t("profile.enabled") : "";
    const note = document.getElementById("profile-scope-note");
    if (note) { note.textContent = p.scopeMode ? ["session", "user"].includes(p.scopeMode) ? this.t("profile.scopeMode." + p.scopeMode) : this.t("profile.scopeMode.unknown", p.scopeMode) : ""; note.classList.toggle("hidden", !p.scopeMode); }
    const list = document.getElementById("profile-users");
    if (list) list.innerHTML = p.listError ? '<div class="inline-feedback" role="alert">' + esc(p.listError) + '<button class="btn btn-sm" data-profile-retry="list">' + esc(this.t("common.retry")) + '</button></div>' : p.users.length ? p.users.map((user, index) => {
      const name = user.sender_name || user.sender_id || this.t("profile.unknownUser");
      const chat = user.last_group_name || (user.last_group_id ? this.t("profile.groupWithId", user.last_group_id) : this.t("profile.privateOrUnknown"));
      return `<button type="button" class="profile-user ${user.identity === p.identity ? "is-selected" : ""}" data-profile-user="${index}" aria-pressed="${user.identity === p.identity}"><span class="profile-avatar" aria-hidden="true">${esc(Array.from(String(name))[0] || "?")}</span><span class="profile-user-copy"><strong>${esc(name)}</strong><span class="profile-user-id">${esc(user.platform || "")} · ${esc(user.sender_id || "--")}</span><span class="profile-user-chat">${esc(chat)}</span><span class="profile-user-count">${esc(this.t("profile.directoryCounts", user.fact_count || 0, user.scope_count || 0))}</span></span></button>`;
    }).join("") : '<div class="table-empty">' + esc(this.t("common.noData")) + '</div>';
    const info = document.getElementById("profile-pagination-info");
    if (info) info.textContent = this.t("profile.userPage", Math.floor(p.offset / p.limit) + 1, Math.max(1, Math.ceil(p.total / p.limit)), p.total);
    const prev = document.getElementById("profile-prev"), next = document.getElementById("profile-next");
    if (prev) prev.disabled = p.offset <= 0;
    if (next) next.disabled = !p.hasMore;
    this._hydrate();
  }
  renderDetail() {
    const container = document.getElementById("profile-reader");
    if (!container) return;
    const p = this.state.profile, u = p.user;
    if (!u) { container.innerHTML = '<div class="profile-reader-empty">' + esc(this.t("profile.selectUser")) + '</div>'; return; }
    const now = Date.now() / 1000;
    const visible = p.items.map((item, index) => ({ item, index })).filter(({ item }) => (!p.category || item.category === p.category) && (!p.lifecycle || this.lifecycle(item, now) === p.lifecycle));
    const scopes = new Map();
    visible.forEach(row => { if (!scopes.has(row.item.profile_scope)) scopes.set(row.item.profile_scope, []); scopes.get(row.item.profile_scope).push(row); });
    let html = `<header class="profile-reader-header"><div><span class="eyebrow">${esc(this.t("profile.detailBadge"))}</span><h2>${esc(u.sender_name || u.sender_id || this.t("profile.unknownUser"))}</h2><p>${esc(u.platform || "")} · ${esc(u.sender_id || "--")}<br>${esc(this.t("profile.recentGroup"))}：${esc(u.last_group_name || (u.last_group_id ? this.t("profile.groupWithId", u.last_group_id) : this.t("profile.privateOrUnknown")))}</p></div><span class="type-tag">${esc(this.t("profile.groupFactCount", p.detailTotal))}</span></header><div class="profile-reader-filters"><label>${esc(this.t("profile.category"))}<select class="select input" id="profile-category"><option value="">${esc(this.t("profile.allCategories"))}</option>${CATEGORIES.map(c => `<option value="${c}"${p.category === c ? " selected" : ""}>${esc(this.t("profile.category." + c))}</option>`).join("")}</select></label><label>${esc(this.t("profile.lifecycle"))}<select class="select input" id="profile-lifecycle"><option value="">${esc(this.t("profile.allLifecycle"))}</option>${["stable", "temporary", "expired", "suppressed"].map(c => `<option value="${c}"${p.lifecycle === c ? " selected" : ""}>${esc(this.t("profile.lifecycle." + c))}</option>`).join("")}</select></label></div>`;
    if (p.detailError) html += '<div class="inline-feedback" role="alert">' + esc(p.detailError) + '<button class="btn btn-sm" data-profile-retry="detail">' + esc(this.t("common.retry")) + '</button></div>';
    if (!visible.length) html += '<div class="table-empty">' + esc(this.t(p.detailLoading ? "common.loading" : "profile.noFacts")) + '</div>';
    for (const rows of scopes.values()) {
      const head = [...rows].sort((a, b) => Number(b.item.updated_at) - Number(a.item.updated_at))[0].item;
      html += `<section class="profile-source-section"><header class="profile-source-header"><i data-lucide="messages-square" aria-hidden="true"></i><div><h3>${esc(this._chatName(head))}</h3><span>${esc(head.source_session_id || this.t("profile.sharedSource"))} · ${esc(this.t("profile.groupFactCount", rows.length))}</span></div></header>`;
      const categories = [...CATEGORIES, ...new Set(rows.map(r => r.item.category).filter(c => !CATEGORIES.includes(c)))];
      for (const category of categories) {
        const facts = rows.filter(r => r.item.category === category);
        if (!facts.length) continue;
        const translated = this.t("profile.category." + category);
        html += `<h4 class="profile-category-heading">${esc(translated.startsWith("profile.category.") ? category || this.t("profile.fact") : translated)}</h4><div class="profile-facts">`;
        facts.forEach(({ item, index }) => {
          const life = this.lifecycle(item, now);
          html += `<article class="profile-fact-card ${life}"><div class="profile-fact-heading"><h5>${esc(this.label(item))}</h5><span class="profile-lifecycle ${life}">${esc(this.t("profile.lifecycle." + life))}</span></div><div class="profile-fact-value">${esc(item.value)}</div><div class="profile-fact-footer"><span>${esc(this._formatTime(item.updated_at))} · ${esc(this.t("profile.confidence"))} ${esc(this._formatConfidence(item.confidence))}</span><button type="button" class="btn btn-secondary btn-sm" data-profile-edit="${index}"><i data-lucide="square-pen" aria-hidden="true"></i>${esc(this.t("profile.readEdit"))}</button></div><details class="profile-fact-source"><summary>${esc(this.t("profile.sourceDetails"))}</summary><dl><dt>${esc(this.t("profile.key"))}</dt><dd>${esc(item.profile_key)}</dd><dt>${esc(this.t("profile.scope"))}</dt><dd>${esc(item.profile_scope)}</dd><dt>${esc(this.t("profile.sourceUser"))}</dt><dd>${esc(item.source_sender_name || "--")}</dd><dt>${esc(this.t("profile.expires"))}</dt><dd>${esc(item.expires_at ? this._formatTime(item.expires_at) : this.t("profile.never"))}</dd></dl>${item.source_memory_id != null ? `<button type="button" class="btn btn-ghost btn-sm" data-source-memory="${esc(item.source_memory_id)}">${esc(this.t("profile.openSource", item.source_memory_id))}<i data-lucide="arrow-up-right" aria-hidden="true"></i></button>` : ""}</details></article>`;
        });
        html += '</div>';
      }
      html += '</section>';
    }
    if (p.detailHasMore) html += `<button type="button" class="btn btn-secondary profile-load-more" id="profile-load-more"${p.detailLoading ? " disabled" : ""}>${esc(this.t(p.detailLoading ? "common.loading" : "profile.loadMore", p.items.length, p.detailTotal))}</button>`;
    container.innerHTML = html; this._hydrate();
  }
  _formatDateInput(value) {
    const n = Number(value); if (!n || !Number.isFinite(n)) return "";
    const d = new Date(n < 1e11 ? n * 1000 : n), pad = v => String(v).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  openItem(item) {
    if (!item || !this.peek) return;
    this.peek._detailGeneration++; this.state._detailCache = null; this.state._nodeDetailCache = null;
    document.getElementById("peek-badge").textContent = this.t("profile.detailBadge");
    document.getElementById("peek-title").textContent = this.label(item);
    const categories = [...new Set([...CATEGORIES, item.category])];
    document.getElementById("peek-body").innerHTML = `<form class="profile-editor" id="profile-editor-form"><div class="profile-editor-section"><div class="peek-section-title">${esc(this.t("profile.editSection"))}</div><label class="form-label" for="profile-edit-category">${esc(this.t("profile.category"))}</label><select class="select input" id="profile-edit-category">${categories.map(c => `<option value="${esc(c)}"${c === item.category ? " selected" : ""}>${esc(this.t("profile.category." + c))}</option>`).join("")}</select><label class="form-label" for="profile-edit-value">${esc(this.t("profile.value"))}</label><textarea class="input textarea" id="profile-edit-value" rows="8" maxlength="300">${esc(item.value)}</textarea><label class="form-label" for="profile-edit-confidence">${esc(this.t("profile.confidence"))}</label><input class="input" id="profile-edit-confidence" type="number" min="0" max="1" step="0.01" value="${esc(item.confidence ?? 1)}"><label class="form-label" for="profile-edit-expires">${esc(this.t("profile.expires"))}</label><input class="input" id="profile-edit-expires" type="datetime-local" value="${esc(this._formatDateInput(item.expires_at))}"><small class="profile-editor-hint">${esc(this.t("profile.expiresHint"))}</small></div><details class="profile-source-details"><summary>${esc(this.t("profile.sourceDetails"))}</summary><div class="profile-detail-row"><span>${esc(this.t("profile.key"))}</span><code>${esc(item.profile_key)}</code></div><div class="profile-detail-row"><span>${esc(this.t("profile.scope"))}</span><code>${esc(item.profile_scope)}</code></div><div class="profile-detail-row"><span>${esc(this.t("profile.sourceSession"))}</span><code>${esc(item.source_session_id || "--")}</code></div>${item.source_memory_id != null ? `<button type="button" class="btn btn-sm" id="profile-editor-source">${esc(this.t("profile.openSource", item.source_memory_id))}</button>` : ""}</details><div class="profile-editor-actions"><button type="button" class="btn btn-danger" id="profile-editor-delete"><i data-lucide="trash-2" aria-hidden="true"></i>${esc(this.t("profile.delete"))}</button><button type="submit" class="btn btn-primary" id="profile-editor-save"><i data-lucide="save" aria-hidden="true"></i>${esc(this.t("common.save"))}</button></div><div id="profile-editor-feedback" class="inline-feedback hidden" role="alert"></div></form>`;
    this.peek.open(true);
    document.getElementById("profile-editor-form")?.addEventListener("submit", event => { event.preventDefault(); this.saveItem(item); });
    document.getElementById("profile-editor-delete")?.addEventListener("click", () => this.deleteItem(item.profile_scope, item.profile_key));
    document.getElementById("profile-editor-source")?.addEventListener("click", () => this.peek.renderMemory({ memory_id: item.source_memory_id }));
    document.getElementById("profile-edit-value")?.focus(); this._hydrate();
  }
  async saveItem(item) {
    if (!item || this._saving) return;
    const value = document.getElementById("profile-edit-value")?.value?.trim() || "", category = document.getElementById("profile-edit-category")?.value || "";
    const text = document.getElementById("profile-edit-confidence")?.value || "", confidence = Number(text);
    const expiresText = document.getElementById("profile-edit-expires")?.value || "", expiry = expiresText ? Date.parse(expiresText) / 1000 : null;
    const feedback = document.getElementById("profile-editor-feedback");
    const fail = message => { if (feedback) { feedback.textContent = message; feedback.classList.remove("hidden"); } this._showToast(message, true); };
    if (!value || !category || !text || !Number.isFinite(confidence) || confidence < 0 || confidence > 1) { fail(this.t("profile.invalidFields")); return; }
    if (expiresText && !Number.isFinite(expiry)) { fail(this.t("profile.invalidExpiry")); return; }
    this._saving = true;
    const button = document.getElementById("profile-editor-save"); if (button) button.disabled = true;
    try {
      await this.api.post("profiles/update", { profile_scope: item.profile_scope, profile_key: item.profile_key, value, category, confidence, expires_at: expiry }, { retries: 0 });
      this._showToast(this.t("profile.updateSuccess")); this.peek.close(); await this.fetch();
    } catch (error) { fail(error.message || this.t("profile.updateFailed")); }
    finally { this._saving = false; if (button) button.disabled = false; }
  }
  async deleteItem(scope, key) {
    if (!scope || !key || this._deleting) return;
    this._deleting = true; this.peek.open(true);
    const editor = document.getElementById("profile-editor-form");
    let cancelled = false;
    try {
      if (!await this.peek.showConfirmDialog(this.t("profile.deleteConfirmTitle"), this.t("profile.deleteConfirmMessage", key, scope))) {
        cancelled = true;
        // Restore the original form node, including live inputs and listeners.
        if (editor) document.getElementById("peek-body")?.replaceChildren(editor);
        return;
      }
      const result = await this.api.post("profiles/delete", { profile_scope: scope, profile_key: key }, { retries: 0 });
      this._showToast(this.t("profile.deleteSuccess", Number(result.count) || 1)); await this.fetch();
    } catch (error) { this._showToast(error.message || this.t("profile.deleteFailed"), true); }
    finally { if (!cancelled) this.peek.close(); this._deleting = false; }
  }
  initEventListeners() {
    if (this._bound) return; this._bound = true;
    const p = this.state.profile, bind = (id, type, fn) => document.getElementById(id)?.addEventListener(type, fn);
    bind("profile-refresh", "click", () => this.fetch());
    bind("profile-query", "input", debounce(event => { p.query = event.target.value.trim(); p.offset = 0; this.fetch(); }, 300));
    bind("profile-session", "input", debounce(event => { p.sessionId = event.target.value.trim(); p.offset = 0; this.fetch(); }, 300));
    bind("profile-reset", "click", () => { p.query = ""; p.sessionId = ""; p.category = ""; p.lifecycle = ""; p.offset = 0; document.getElementById("profile-query").value = ""; document.getElementById("profile-session").value = ""; this.fetch(); });
    bind("profile-prev", "click", () => { if (p.offset > 0) { p.offset = Math.max(0, p.offset - p.limit); this.fetch(); } });
    bind("profile-next", "click", () => { if (p.hasMore) { p.offset += p.limit; this.fetch(); } });
    bind("profile-users", "click", event => { const user = event.target.closest("[data-profile-user]"); if (user) this.selectUser(p.users[Number(user.dataset.profileUser)], true); if (event.target.closest('[data-profile-retry="list"]')) this.fetch(); });
    bind("profile-reader", "click", event => {
      const edit = event.target.closest("[data-profile-edit]"); if (edit) this.openItem(p.items[Number(edit.dataset.profileEdit)]);
      const source = event.target.closest("[data-source-memory]"); if (source) this.peek.renderMemory({ memory_id: source.dataset.sourceMemory });
      if (event.target.closest("#profile-load-more")) this.fetchDetail(true);
      if (event.target.closest('[data-profile-retry="detail"]')) this.fetchDetail(p.items.length > 0);
    });
    bind("profile-reader", "change", event => { if (event.target.id === "profile-category") p.category = event.target.value; else if (event.target.id === "profile-lifecycle") p.lifecycle = event.target.value; else return; this.renderDetail(); });
  }
}
